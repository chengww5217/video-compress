import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import tempfile
import tomllib
import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress import config, probe


def flat_keys(table: dict, prefix: str = "") -> set:
    """把嵌套表压成 "a.b.c" 的键集合，方便做结构比对。"""
    out = set()
    for key, value in table.items():
        here = f"{prefix}{key}"
        out.add(here)
        if isinstance(value, dict):
            out |= flat_keys(value, here + ".")
    return out


class ShippedConfigTests(unittest.TestCase):
    """随包发布的 config.toml 必须和代码内置的默认值对得上。"""

    @classmethod
    def setUpClass(cls):
        with open(helpers.ROOT / "config.toml", "rb") as f:
            cls.raw = tomllib.load(f)

    def test_structure_matches_defaults_exactly(self):
        self.assertEqual(flat_keys(self.raw), flat_keys(config.DEFAULTS),
                         "config.toml drifted from config.DEFAULTS")

    def test_shipped_config_passes_validation(self):
        self.assertIsInstance(config.normalise(self.raw), dict)

    def test_defaults_pass_validation(self):
        self.assertIsInstance(config.normalise({}), dict)

    def test_min_saving_ratio_lives_under_skip(self):
        self.assertIn("min_saving_ratio", self.raw["skip"])
        self.assertNotIn("min_saving_ratio", self.raw["skip"]["expected_ratio"])

    def test_ratio_and_bpp_tables_hold_only_codec_entries(self):
        families = set(probe.CODEC_FAMILY.values())
        for table in ("bpp", "expected_ratio"):
            for key, value in self.raw["skip"][table].items():
                self.assertIn(key, families, f"{table}.{key} is not a codec")
                self.assertIsInstance(value, float, f"{table}.{key}")

    def test_min_saving_ratio_actually_gates_skipping(self):
        cfg = config.normalise(self.raw)
        info = make_info(bitrate=20_000_000)
        skip, reason = probe.should_skip(info, cfg)
        self.assertFalse(skip, reason)

        cfg["skip"]["min_saving_ratio"] = 0.9
        skip, reason = probe.should_skip(info, cfg)
        self.assertTrue(skip, "raising min_saving_ratio must skip more files")
        self.assertIn("90%", reason)


class MergeTests(unittest.TestCase):
    def test_missing_sections_are_filled_in(self):
        cfg = config.normalise({"encode": {"encoder": "x265"}})
        self.assertEqual(cfg["encode"]["encoder"], "x265")
        self.assertEqual(cfg["run"]["workers"],
                         config.DEFAULTS["run"]["workers"])
        self.assertIn("bpp", cfg["skip"])

    def test_partial_override_keeps_sibling_defaults(self):
        cfg = config.normalise({"verify": {"vmaf_threshold": 90.0}})
        self.assertEqual(cfg["verify"]["vmaf_threshold"], 90.0)
        self.assertTrue(cfg["verify"]["decode_check"])

    def test_defaults_are_not_mutated_by_callers(self):
        cfg = config.defaults()
        cfg["skip"]["extensions"].append(".xyz")
        cfg["skip"]["bpp"]["h264"] = 99.0
        self.assertNotIn(".xyz", config.DEFAULTS["skip"]["extensions"])
        self.assertNotEqual(config.DEFAULTS["skip"]["bpp"]["h264"], 99.0)

    def test_extensions_and_container_are_lowercased(self):
        cfg = config.normalise({
            "skip": {"extensions": [".MP4", ".MKV"]},
            "encode": {"output_container": ".MKV"},
        })
        self.assertEqual(cfg["skip"]["extensions"], [".mp4", ".mkv"])
        self.assertEqual(cfg["encode"]["output_container"], ".mkv")

    def test_custom_encoder_table_is_accepted(self):
        cfg = config.normalise({
            "encode": {
                "encoder": "vp9",
                "vp9": {"codec": "libvpx-vp9", "preset": "1", "crf": 31},
            },
        })
        self.assertIn("vp9", config.encoder_names(cfg))
        self.assertEqual(cfg["encode"]["vp9"]["extra"], "")


class ValidationTests(unittest.TestCase):
    """配置写错时要报出**哪一个键**错了，而不是甩一个 KeyError。"""

    def assert_rejected(self, user_cfg, *needles):
        with self.assertRaises(config.ConfigError) as cm:
            config.normalise(user_cfg)
        msg = str(cm.exception)
        for needle in needles:
            self.assertIn(needle, msg)
        return msg

    def test_typo_in_option_name_is_rejected(self):
        self.assert_rejected({"skip": {"min_saving_ration": 0.2}},
                             "这个配置项", "min_saving_ration")

    def test_unknown_section_is_rejected(self):
        self.assert_rejected({"encoding": {}}, "这个配置节", "encoding")

    def test_wrong_type_is_rejected(self):
        self.assert_rejected({"run": {"workers": "many"}}, "workers", "int")

    def test_selected_encoder_must_have_a_table(self):
        self.assert_rejected({"encode": {"encoder": "vp9"}},
                             "[encode.vp9]", "可用的有")

    def test_encoder_table_needs_a_codec(self):
        self.assert_rejected(
            {"encode": {"encoder": "raw", "raw": {"preset": "1", "crf": 0}}},
            "[encode.raw] 缺少必填项 codec")

    def test_unknown_key_inside_encoder_table_is_rejected(self):
        self.assert_rejected(
            {"encode": {"av1": {"pixel_format": "yuv420p"}}},
            "这个配置项", "pixel_format")

    def test_ratio_out_of_range_is_rejected(self):
        self.assert_rejected({"skip": {"min_saving_ratio": 1.5}},
                             "必须在 0 和 1 之间")

    def test_crf_bounds_must_be_ordered(self):
        self.assert_rejected({"adaptive": {"crf_min": 40, "crf_max": 20}},
                             "crf_min 必须小于 crf_max")

    def test_empty_tag_marker_is_rejected(self):
        self.assert_rejected({"skip": {"tag_marker": "  "}}, "tag_marker")

    def test_extension_without_dot_is_rejected(self):
        self.assert_rejected({"skip": {"extensions": ["mp4"]}},
                             "必须以点开头")

    def test_zero_workers_is_rejected(self):
        self.assert_rejected({"run": {"workers": 0}}, "至少是 1")

    def test_out_of_range_vmaf_threshold_is_rejected(self):
        self.assert_rejected({"verify": {"vmaf_threshold": 120.0}},
                             "vmaf_threshold")

    def test_bpp_table_values_must_be_numbers(self):
        self.assert_rejected({"skip": {"bpp": {"h264": "low"}}},
                             "必须是数字")

    def test_integers_are_accepted_where_floats_are_expected(self):
        cfg = config.normalise({"adaptive": {"target_vmaf": 95}})
        self.assertEqual(cfg["adaptive"]["target_vmaf"], 95)

    def test_out_of_range_target_margin_is_rejected(self):
        self.assert_rejected({"adaptive": {"target_margin": -0.1}},
                             "target_margin")
        self.assert_rejected({"adaptive": {"target_margin": 2.0}},
                             "target_margin")

    def test_all_problems_are_reported_at_once(self):
        msg = self.assert_rejected(
            {"run": {"workers": "many"}, "skip": {"typo": 1}})
        self.assertEqual(msg.count("\n  - "), 1, msg)
        self.assertIn("workers", msg)
        self.assertIn("typo", msg)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_required_file_raises(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.load(self.root / "nope.toml", required=True)
        self.assertIn("不存在", str(cm.exception))

    def test_missing_optional_file_falls_back_to_defaults(self):
        cfg = config.load(self.root / "nope.toml", required=False)
        self.assertEqual(cfg["encode"]["encoder"],
                         config.DEFAULTS["encode"]["encoder"])

    def test_broken_toml_names_the_file(self):
        p = self.root / "bad.toml"
        p.write_text("this is = = not toml\n", encoding="utf-8")
        with self.assertRaises(config.ConfigError) as cm:
            config.load(p)
        self.assertIn("bad.toml", str(cm.exception))

    def test_bad_option_message_names_the_file(self):
        p = self.root / "c.toml"
        p.write_text("[run]\nworkers = 0\n", encoding="utf-8")
        with self.assertRaises(config.ConfigError) as cm:
            config.load(p)
        self.assertIn("c.toml", str(cm.exception))
        self.assertIn("workers", str(cm.exception))


class ResolveRunPathTests(unittest.TestCase):
    def test_relative_path_hangs_off_the_config_dir(self):
        base = Path("/etc/vcompress")
        self.assertEqual(config.resolve_run_path(base, "logs/results.jsonl"),
                         base / "logs" / "results.jsonl")

    def test_absolute_path_kept(self):
        base = Path("/etc/vcompress")
        abs_p = Path(tempfile.gettempdir()).resolve() / "r.jsonl"
        self.assertEqual(config.resolve_run_path(base, str(abs_p)), abs_p)

    def test_user_home_expanded(self):
        got = config.resolve_run_path(Path("/etc/vcompress"), "~/vc.log")
        self.assertEqual(got, Path.home() / "vc.log")


class FixtureTests(unittest.TestCase):
    def test_fixture_is_a_valid_config(self):
        self.assertIsInstance(config.validate(copy.deepcopy(make_cfg())), dict)


if __name__ == "__main__":
    unittest.main()
