"""构建能力探测与像素格式决策。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

from helpers import make_cfg
from vcompress import caps, encode, vmaf

ENCODERS_SAMPLE = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC (codec h264)
 V..... libsvtav1            SVT-AV1 encoder (codec av1)
 A....D libopus              libopus Opus (codec opus)
 S..... srt                  SubRip subtitle
"""

FILTERS_SAMPLE = """Filters:
  T.. = Timeline support
 ... libvmaf           VV->V      Calculate the VMAF between two streams.
 ..C scale             V->V       Scale the input video size.
 ... amix              N->A       Audio mixing.
"""


class BuildCapabilityTests(unittest.TestCase):
    def test_encoder_names_parsed(self):
        names = caps._parse_encoder_names(ENCODERS_SAMPLE)
        self.assertEqual(names, {"libx264", "libsvtav1", "libopus", "srt"})

    def test_encoder_header_lines_ignored(self):
        self.assertNotIn("=", caps._parse_encoder_names(ENCODERS_SAMPLE))

    def test_filter_names_parsed(self):
        names = caps._parse_filter_names(FILTERS_SAMPLE)
        self.assertEqual(names, {"libvmaf", "scale", "amix"})

    def test_missing_binary_yields_empty_sets(self):
        self.assertEqual(caps.available_encoders("no-such-ffmpeg-xyz"), set())
        self.assertEqual(caps.available_filters("no-such-ffmpeg-xyz"), set())

class BitDepthBumpTests(unittest.TestCase):
    """8-bit 源用 10-bit 编码：精度更高、暗部渐变不容易 banding，码率还略低。"""

    SVT = {"yuv420p", "yuv420p10le"}
    X265 = {"yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p",
            "yuv444p10le", "yuv420p12le"}

    def test_eight_bit_is_bumped(self):
        self.assertEqual(caps.bumped_pix_fmt("yuv420p", self.SVT, 10),
                         "yuv420p10le")

    def test_chroma_subsampling_is_preserved(self):
        """4:2:2 源在支持它的编码器上只提位深，不能顺手压成 4:2:0。"""
        self.assertEqual(caps.bumped_pix_fmt("yuv422p", self.X265, 10),
                         "yuv422p10le")
        self.assertEqual(caps.bumped_pix_fmt("yuv444p", self.X265, 10),
                         "yuv444p10le")

    def test_already_deep_enough_is_left_alone(self):
        self.assertIsNone(caps.bumped_pix_fmt("yuv420p10le", self.SVT, 10))
        self.assertIsNone(caps.bumped_pix_fmt("yuv420p12le", self.X265, 10))

    def test_disabled_by_min_depth_eight(self):
        self.assertIsNone(caps.bumped_pix_fmt("yuv420p", self.SVT, 8))

    def test_falls_back_to_a_deeper_format_when_needed(self):
        self.assertEqual(
            caps.bumped_pix_fmt("yuv420p", {"yuv420p", "yuv420p12le"}, 10),
            "yuv420p12le")

    def test_unsupported_bump_changes_nothing(self):
        self.assertIsNone(caps.bumped_pix_fmt("yuv420p", {"yuv420p"}, 10))

    def test_exotic_formats_are_left_alone(self):
        for fmt in ("nv12", "p010le", "rgb24", "gray", "yuvj420p", ""):
            self.assertIsNone(
                caps.bumped_pix_fmt(fmt, self.SVT | {"nv12", "gray"}, 10),
                fmt)

    def test_with_pix_fmt_bumps_a_supported_source(self):
        cfg = caps.with_pix_fmt(make_cfg()["encode"]["av1"], "yuv420p",
                                  self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_explicit_pix_fmt_still_wins(self):
        base = dict(make_cfg()["encode"]["av1"], pix_fmt="yuv420p")
        cfg = caps.with_pix_fmt(base, "yuv420p", self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p")

class PixFmtTests(unittest.TestCase):
    """libsvtav1 只吃 4:2:0；不归一化的话 4:2:2 源（ProRes、部分 mov）直接失败。"""

    SVT = {"yuv420p", "yuv420p10le"}
    X265 = {"yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p"}

    def test_supported_format_is_left_alone(self):
        self.assertIsNone(caps.choose_pix_fmt("yuv420p", self.SVT))
        self.assertIsNone(caps.choose_pix_fmt("yuv420p10le", self.SVT))

    def test_unknown_capabilities_change_nothing(self):
        self.assertIsNone(caps.choose_pix_fmt("yuv422p", set()))
        self.assertIsNone(caps.choose_pix_fmt("", self.SVT))

    def test_bit_depth_is_preserved_when_subsampling(self):
        self.assertEqual(caps.choose_pix_fmt("yuv422p10le", self.SVT),
                         "yuv420p10le")
        self.assertEqual(caps.choose_pix_fmt("yuv444p10le", self.SVT),
                         "yuv420p10le")

    def test_eight_bit_stays_eight_bit(self):
        self.assertEqual(caps.choose_pix_fmt("yuv422p", self.SVT), "yuv420p")

    def test_higher_depth_degrades_to_the_closest_available(self):
        self.assertEqual(caps.choose_pix_fmt("yuv420p12le", self.SVT),
                         "yuv420p10le")

    def test_x265_keeps_422(self):
        self.assertIsNone(caps.choose_pix_fmt("yuv422p", self.X265))

    def test_depth_parsing(self):
        self.assertEqual(caps.pix_fmt_depth("yuv420p"), 8)
        self.assertEqual(caps.pix_fmt_depth("yuv420p10le"), 10)
        self.assertEqual(caps.pix_fmt_depth("yuv444p12be"), 12)
        self.assertEqual(caps.pix_fmt_depth(""), 8)

    def test_with_pix_fmt_adds_the_flag_and_warns(self):
        warnings = []
        log = _FakeLog(warnings)
        base = dict(make_cfg()["encode"]["av1"], min_bit_depth=8)
        cfg = caps.with_pix_fmt(base, "yuv422p", self.SVT, log)
        self.assertEqual(cfg["pix_fmt"], "yuv420p")
        self.assertIn("yuv422p", warnings[0])
        self.assertIn("-pix_fmt", encode.video_args(cfg, 30))
        self.assertEqual(encode.video_args(cfg, 30)[7], "yuv420p")

    def test_subsampled_source_is_also_bumped_to_10_bit(self):
        """4:2:2 8-bit 源：色度降到 4:2:0，位深顺手提到 10-bit。"""
        cfg = caps.with_pix_fmt(make_cfg()["encode"]["av1"], "yuv422p",
                                  self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_with_pix_fmt_respects_an_explicit_setting(self):
        base = dict(make_cfg()["encode"]["av1"], pix_fmt="yuv420p10le")
        cfg = caps.with_pix_fmt(base, "yuv422p", self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_with_pix_fmt_does_not_mutate_the_input(self):
        base = make_cfg()["encode"]["av1"]
        caps.with_pix_fmt(base, "yuv422p", self.SVT, None)
        self.assertNotIn("pix_fmt", base)

    def test_hopeless_format_warns_instead_of_guessing(self):
        warnings = []
        cfg = caps.with_pix_fmt(make_cfg()["encode"]["av1"], "rgb24",
                                  {"gray"}, _FakeLog(warnings))
        self.assertNotIn("pix_fmt", cfg)
        self.assertIn("may fail", warnings[0])

class _FakeLog:
    def __init__(self, sink):
        self.sink = sink

    def warning(self, msg):
        self.sink.append(str(msg))

    def info(self, msg):
        pass

    def debug(self, msg):
        pass

class CapsTests(unittest.TestCase):
    def test_x264_is_preferred_for_sampling(self):
        built = caps.Caps(encoders=frozenset({"libx264", "libsvtav1"}))
        self.assertEqual(built.lossless_codec, "libx264")

    def test_ffv1_is_the_fallback(self):
        built = caps.Caps(encoders=frozenset({"libsvtav1"}))
        self.assertEqual(built.lossless_codec, "ffv1")

    def test_unknown_build_assumes_x264(self):
        self.assertFalse(caps.Caps().known)
        self.assertEqual(caps.Caps().lossless_codec, "libx264")

    def test_lossless_args(self):
        self.assertEqual(vmaf.lossless_args("libx264"),
                         ["-c:v", "libx264", "-crf", "0", "-preset", "ultrafast"])
        self.assertEqual(vmaf.lossless_args("ffv1"),
                         ["-c:v", "ffv1", "-level", "1"])


if __name__ == "__main__":
    unittest.main()
