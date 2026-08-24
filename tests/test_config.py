import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import copy
import tomllib
import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress import probe


class ShippedConfigTests(unittest.TestCase):
    """随包发布的 config.toml 必须和代码实际读取的结构对得上。"""

    @classmethod
    def setUpClass(cls):
        with open(helpers.ROOT / "config.toml", "rb") as f:
            cls.cfg = tomllib.load(f)

    def test_min_saving_ratio_lives_under_skip(self):
        self.assertIn("min_saving_ratio", self.cfg["skip"])
        self.assertNotIn("min_saving_ratio", self.cfg["skip"]["expected_ratio"])

    def test_ratio_and_bpp_tables_hold_only_codec_entries(self):
        families = set(probe.CODEC_FAMILY.values())
        for table in ("bpp", "expected_ratio"):
            for key, value in self.cfg["skip"][table].items():
                self.assertIn(key, families, f"{table}.{key} is not a codec")
                self.assertIsInstance(value, float, f"{table}.{key}")

    def test_sections_match_the_test_fixture(self):
        fixture = make_cfg()
        for section in fixture:
            self.assertEqual(set(self.cfg[section]), set(fixture[section]),
                             f"[{section}] drifted from the fixture")

    def test_min_saving_ratio_actually_gates_skipping(self):
        cfg = copy.deepcopy(self.cfg)
        info = make_info(bitrate=20_000_000)
        skip, reason = probe.should_skip(info, cfg)
        self.assertFalse(skip, reason)

        cfg["skip"]["min_saving_ratio"] = 0.9
        skip, reason = probe.should_skip(info, cfg)
        self.assertTrue(skip, "raising min_saving_ratio must skip more files")
        self.assertIn("90%", reason)


if __name__ == "__main__":
    unittest.main()
