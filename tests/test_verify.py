import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress import encode, verify
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


class VmafSpotCheckTests(unittest.TestCase):
    """用替身挡掉 ffmpeg，只验证参数处理与结果判定。"""

    def setUp(self):
        self.cfg = make_cfg()
        self.cfg["verify"]["vmaf_segments"] = 2
        self.cfg["verify"]["vmaf_sample_seconds"] = 5
        self.extracted = []
        self.scores = [96.0, 94.0]
        self.orig_extract = encode.extract_segment
        self.orig_score = encode.vmaf_score
        encode.extract_segment = lambda ff, src, start, secs, out: \
            self.extracted.append((Path(src).name, round(start, 3)))
        encode.vmaf_score = lambda ff, dist, ref: \
            self.scores[len(self.extracted) // 2 - 1]

    def tearDown(self):
        encode.extract_segment = self.orig_extract
        encode.vmaf_score = self.orig_score

    def check(self, duration=600.0):
        return verify.vmaf_spot_check(FakeTools(), Path("a.mkv"),
                                      Path("b.mkv"), duration, self.cfg)

    def test_disabled_is_not_reported_as_unavailable(self):
        self.cfg["verify"]["vmaf_enabled"] = False
        self.assertEqual(self.check(), (None, ""))
        self.assertEqual(self.extracted, [])

    def test_mean_above_threshold_passes(self):
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 95.0)
        self.assertEqual(msg, "")
        self.assertEqual(len(self.extracted), 4, "2 segments x ref+dist")

    def test_mean_below_threshold_reports_every_segment(self):
        self.scores = [80.0, 82.0]
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 81.0)
        self.assertIn("below threshold", msg)
        self.assertIn("80.00", msg)
        self.assertIn("82.00", msg)

    def test_zero_segments_still_measures_once(self):
        self.cfg["verify"]["vmaf_segments"] = 0
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 96.0)
        self.assertEqual(msg, "")

    def test_garbage_segment_count_falls_back_to_default(self):
        self.cfg["verify"]["vmaf_segments"] = "three"
        self.scores = [96.0, 96.0, 96.0]
        mean, _ = self.check()
        self.assertEqual(len(self.extracted), 6)
        self.assertAlmostEqual(mean, 96.0)

    def test_unknown_duration_is_reported(self):
        mean, msg = self.check(duration=0.0)
        self.assertIsNone(mean)
        self.assertIn("duration", msg)
        self.assertEqual(self.extracted, [])

    def test_ffmpeg_failure_is_surfaced_not_raised(self):
        def boom(*a):
            raise encode.EncodeError("libvmaf failed: no such filter")

        encode.vmaf_score = boom
        mean, msg = self.check()
        self.assertIsNone(mean)
        self.assertIn("no such filter", msg)

    def test_sample_never_starts_past_the_end(self):
        self.check(duration=8.0)
        for _name, start in self.extracted:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(start, 8.0)


class FakeTools:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"


if __name__ == "__main__":
    unittest.main()

