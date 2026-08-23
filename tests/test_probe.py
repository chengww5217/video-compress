import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress.probe import _parse_fps, _to_int, should_skip


class ParseHelperTests(unittest.TestCase):
    def test_parse_fps_fraction(self):
        self.assertAlmostEqual(_parse_fps("30000/1001"), 29.97, places=2)

    def test_parse_fps_zero_denominator(self):
        self.assertEqual(_parse_fps("30/0"), 0.0)

    def test_parse_fps_garbage(self):
        self.assertEqual(_parse_fps("abc"), 0.0)
        self.assertEqual(_parse_fps(""), 0.0)

    def test_to_int_variants(self):
        self.assertEqual(_to_int("123"), 123)
        self.assertEqual(_to_int("12.9"), 12)
        self.assertEqual(_to_int(None, -1), -1)
        self.assertEqual(_to_int("not-a-number"), 0)


class ShouldSkipTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_tagged_file_skips(self):
        skip, reason = should_skip(make_info(tagged=True), self.cfg)
        self.assertTrue(skip)
        self.assertIn("processed", reason)

    def test_small_file_skips(self):
        skip, reason = should_skip(make_info(size=1024), self.cfg)
        self.assertTrue(skip)
        self.assertIn("small", reason)

    def test_bpp_below_threshold_skips(self):
        info = make_info(bitrate=3_000_000)
        bpp = 3_000_000 / (1920 * 1080 * 30)
        self.assertLess(bpp, 0.05)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)
        self.assertIn("threshold", reason)

    def test_healthy_h264_not_skipped(self):
        skip, reason = should_skip(make_info(), self.cfg)
        self.assertFalse(skip, reason)

    def test_low_bitrate_hevc_skips(self):
        info = make_info(video_codec="hevc", codec_family="hevc", bitrate=1_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)

    def test_av1_source_skips_on_predicted_saving(self):
        info = make_info(video_codec="av01", codec_family="av1", bitrate=20_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)
        self.assertIn("saving", reason)

    def test_unknown_codec_not_skipped(self):
        info = make_info(video_codec="prores", codec_family="", bitrate=100_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertFalse(skip, reason)

    def test_unknown_bitrate_skips_bpp_gate_only(self):
        info = make_info(bitrate=0)
        skip, reason = should_skip(info, self.cfg)
        self.assertFalse(skip, reason)


if __name__ == "__main__":
    unittest.main()
