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
from vcompress import adaptive, encode, proc, vmaf


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


class PickCrfMarginTests(unittest.TestCase):
    """贴线的搜索结果要自动降一档：样本外推到全片的波动不能把宝押在运气上。

    假想的片源：crf ≤ 31 时最差段 95.5，31 < crf ≤ 32 时 95.02（贴线），
    更高直接崩到 94。余量 0 时 32 会被录取；余量 0.05 时只能录到 31。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.orig_encode = adaptive._encode_sample
        self.orig_score = vmaf.measure
        self.orig_extract = vmaf.extract_segment
        adaptive._encode_sample = self.fake_encode
        vmaf.measure = self.fake_score
        vmaf.extract_segment = self.fake_extract
        self.last_crf = None
        self.log = logging.getLogger("pick-crf-test")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self):
        adaptive._encode_sample = self.orig_encode
        vmaf.measure = self.orig_score
        vmaf.extract_segment = self.orig_extract
        self.tmp.cleanup()

    def fake_extract(self, ff, src, start, seconds, out, codec="libx264"):
        Path(out).write_bytes(b"ref")

    def fake_encode(self, ff, ref, crf, cfg, out):
        self.last_crf = crf
        Path(out).write_bytes(b"x" * 1000)

    def fake_score(self, ff, dist, ref, shift=0, **kw):
        crf = self.last_crf
        if crf <= 31:
            return 95.5
        if crf <= 32:
            return 95.02
        return 94.0

    def pick(self, margin):
        cfg = make_cfg()
        ad = cfg["adaptive"]
        ad["target_margin"] = margin
        ad["sample_count"] = 2
        return adaptive.pick_crf(helpers.FakeTools(), self.root / "a.mp4",
                                 600.0, cfg["encode"]["av1"], ad, self.log)

    def test_zero_margin_keeps_the_borderline_result(self):
        picked = self.pick(0.0)
        self.assertEqual(picked.crf, 32)
        self.assertTrue(picked.meets_target)

    def test_borderline_result_steps_down_a_notch(self):
        picked = self.pick(0.05)
        self.assertEqual(picked.crf, 31)


class CancelDuringSearchTests(unittest.TestCase):
    """Ctrl+C 打断样本编码，不能被当成「搜索失败」回退到固定 CRF。

    真实日志里的后果：汇总都打完了，两个 worker 又各自起了一个几小时的编码，
    还写出了临时文件。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.orig_encode = adaptive._encode_sample
        self.orig_extract = vmaf.extract_segment
        adaptive._encode_sample = self.cancel_then_fail
        vmaf.extract_segment = self.fake_extract
        self.log = logging.getLogger("cancel-search-test")
        self.log.addHandler(logging.NullHandler())

    def tearDown(self):
        adaptive._encode_sample = self.orig_encode
        vmaf.extract_segment = self.orig_extract
        proc.clear_cancel()
        self.tmp.cleanup()

    def fake_extract(self, ff, src, start, seconds, out, codec="libx264"):
        Path(out).write_bytes(b"ref")

    def cancel_then_fail(self, ff, ref, crf, cfg, out):
        # 用户按了 Ctrl+C：主线程 terminate_all 杀掉样本编码，子进程返回非 0
        proc.terminate_all()
        raise encode.EncodeError("sample encode failed at crf=33: killed")

    def test_cancel_propagates_instead_of_falling_back(self):
        cfg = make_cfg()
        with self.assertRaises(proc.Cancelled):
            adaptive.pick_crf(helpers.FakeTools(), self.root / "a.mp4", 600.0,
                              cfg["encode"]["av1"], cfg["adaptive"], self.log)

    def test_plain_failure_still_falls_back(self):
        """没有取消的普通失败仍然回退固定 CRF——那个行为是对的。"""
        adaptive._encode_sample = self.orig_encode

        def just_fail(ff, ref, crf, cfg, out):
            raise encode.EncodeError("something else broke")

        adaptive._encode_sample = just_fail
        cfg = make_cfg()
        self.assertIsNone(
            adaptive.pick_crf(helpers.FakeTools(), self.root / "a.mp4", 600.0,
                              cfg["encode"]["av1"], cfg["adaptive"], self.log))


class SampleCommandTests(unittest.TestCase):
    """样本编码必须关掉进度输出，否则失败原因会被进度行挤出错误信息。"""

    def setUp(self):
        self.cmds = []
        self.rc = 0
        self.orig_run = adaptive.proc.run_managed
        adaptive.proc.run_managed = self.fake_run

    def tearDown(self):
        adaptive.proc.run_managed = self.orig_run

    def fake_run(self, cmd, cwd=None):
        self.cmds.append(cmd)
        return self.rc, "", ("frame=  144 fps= 22 speed=0.734x\n"
                             "No space left on device\n"
                             "[libx264 @ 0x1] kb/s:497611.09\n"
                             "Conversion failed!")

    def test_nostats_is_passed(self):
        adaptive._encode_sample("ffmpeg", Path("ref.mkv"), 30,
                                make_cfg()["encode"]["av1"], Path("out.mkv"))
        self.assertIn("-nostats", self.cmds[0])

    def test_failure_message_keeps_the_real_cause(self):
        self.rc = 1
        with self.assertRaises(encode.EncodeError) as cm:
            adaptive._encode_sample("ffmpeg", Path("ref.mkv"), 30,
                                    make_cfg()["encode"]["av1"],
                                    Path("out.mkv"))
        self.assertIn("No space left on device", str(cm.exception))
        self.assertNotIn("frame=", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
