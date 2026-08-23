import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress.verify import check_basic


class CheckBasicTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.orig = make_info(duration=600.0)

    def test_happy_path(self):
        out = make_info(video_codec="av1", duration=600.2)
        ok, msg = check_basic(self.orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertTrue(ok, msg)
        self.assertEqual(msg, "")

    def test_duration_mismatch_fails(self):
        out = make_info(video_codec="av1", duration=601.5)
        ok, msg = check_basic(self.orig, out, 100, 50, self.cfg)
        self.assertFalse(ok)
        self.assertIn("duration", msg)

    def test_missing_video_stream_fails(self):
        out = make_info(video_codec="", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100, 50, self.cfg)
        self.assertFalse(ok)
        self.assertIn("video stream", msg)

    def test_output_not_smaller_fails(self):
        out = make_info(video_codec="av1", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100, 101, self.cfg)
        self.assertFalse(ok)
        self.assertIn(">=", msg)

    def test_tiny_saving_discarded(self):
        out = make_info(video_codec="av1", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100_000_000, 98_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("discarding", msg)


if __name__ == "__main__":
    unittest.main()

