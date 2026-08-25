"""自适应 CRF 搜索。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import make_cfg
from vcompress import adaptive, encode, vmaf


class SearchMidpointTests(unittest.TestCase):
    def test_quarter_step_search_converges(self):
        lo, hi, step = 16, 50, 0.25
        seen = []
        while hi - lo > step:
            mid = adaptive.search_midpoint(lo, hi, step)
            self.assertLess(lo, mid)
            self.assertLess(mid, hi)
            seen.append(mid)
            hi = mid          # 一直往下收，最坏情况
            self.assertLess(len(seen), 60, "search must terminate")
        self.assertEqual(encode.quantize_crf(lo + step), encode.quantize_crf(hi))

    def test_midpoint_lands_on_the_grid(self):
        for _ in range(1):
            mid = adaptive.search_midpoint(16, 50, 0.25)
            self.assertEqual(mid, encode.quantize_crf(mid))

    def test_coarse_step_snaps_to_that_grid(self):
        """粗步长时中点落在步长网格上（33 不在 2 的网格上，取 32）。"""
        self.assertEqual(adaptive.search_midpoint(16, 50, 2), 32)

    def test_narrow_range_stays_inside(self):
        mid = adaptive.search_midpoint(35, 35.5, 0.25)
        self.assertEqual(mid, 35.25)

class ProbeCrfTests(unittest.TestCase):
    """一个 CRF 的分数取最差段，而不是平均。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.refs = [self.root / f"ref{i}.mkv" for i in range(3)]
        self.encoded = []
        self.orig_encode = adaptive._encode_sample
        self.orig_score = vmaf.measure
        adaptive._encode_sample = self.fake_encode
        vmaf.measure = self.fake_score
        self.log = logging.getLogger("probe-crf-test")
        self.log.addHandler(logging.NullHandler())

    def fake_encode(self, ff, ref, crf, cfg, out):
        self.encoded.append(Path(ref).name)
        Path(out).write_bytes(b"x" * 1000)

    def tearDown(self):
        adaptive._encode_sample = self.orig_encode
        vmaf.measure = self.orig_score
        self.tmp.cleanup()

    def fake_score(self, ff, dist, ref, shift=0, **kw):
        return self.scores[len(self.encoded) - 1]

    def probe(self, crf=30, target=95.0):
        return adaptive._probe_crf(helpers.FakeTools(), self.refs, crf, {},
                                 self.root, target, "", self.log)

    def test_worst_segment_decides(self):
        self.scores = [99.0, 96.0, 95.5]
        worst, total, measured = self.probe()
        self.assertAlmostEqual(worst, 95.5)
        self.assertEqual((total, measured), (3000, 3))
        self.assertEqual(len(self.encoded), 3)

    def test_stops_at_the_first_failing_segment(self):
        self.scores = [99.0, 80.0, 99.0]
        worst, total, measured = self.probe()
        self.assertAlmostEqual(worst, 80.0)
        self.assertEqual((total, measured), (2000, 2))
        self.assertEqual(len(self.encoded), 2,
                         "already below target, no point measuring the rest")

    def test_samples_are_deleted_after_measuring(self):
        self.scores = [99.0, 99.0, 99.0]
        self.probe()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [])

class SampleBitrateTests(unittest.TestCase):
    def test_bytes_over_total_sample_seconds(self):
        self.assertEqual(adaptive._sample_bitrate(1_000_000, 10.0, 4), 200_000)

    def test_degenerate_inputs(self):
        self.assertEqual(adaptive._sample_bitrate(0, 10.0, 4), 0)
        self.assertEqual(adaptive._sample_bitrate(1000, 0.0, 4), 0)
        self.assertEqual(adaptive._sample_bitrate(1000, 10.0, 0), 0)

class AdaptiveFallbackTests(unittest.TestCase):
    def test_missing_binary_returns_none(self):
        import logging

        log = logging.getLogger("test")
        log.addHandler(logging.NullHandler())
        class FakeTools:
            ffmpeg = "definitely-not-a-real-binary-xyz"
            ffprobe = "definitely-not-a-real-binary-xyz"

        picked = adaptive.pick_crf(
            FakeTools(), Path("whatever.mp4"), 60.0,
            make_cfg()["encode"]["av1"],
            make_cfg()["adaptive"], log,
        )
        self.assertIsNone(picked)


if __name__ == "__main__":
    unittest.main()
