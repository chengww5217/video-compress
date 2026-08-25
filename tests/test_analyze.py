import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress import analyze
from vcompress.analyze import Scan, make_plan, parse_scan

IDET_INTERLACED = [
    "[Parsed_idet_0 @ 0x1] Repeated Fields: Neither:   250 Top:     0 Bottom:     0",
    "[Parsed_idet_0 @ 0x1] Single frame detection: TFF:   250 BFF:     0 "
    "Progressive:     0 Undetermined:     0",
    "[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:   250 BFF:     0 "
    "Progressive:     0 Undetermined:     0",
]
IDET_PROGRESSIVE = [
    "[Parsed_idet_0 @ 0x1] Repeated Fields: Neither:   500 Top:     0 Bottom:     0",
    "[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:     0 BFF:     0 "
    "Progressive:   500 Undetermined:     0",
]
IDET_TELECINE = [
    "[Parsed_idet_0 @ 0x1] Repeated Fields: Neither:   300 Top:   100 Bottom:   100",
    "[Parsed_idet_0 @ 0x1] Multi frame detection: TFF:   400 BFF:     0 "
    "Progressive:   100 Undetermined:     0",
]
CROP_LINE = ("[Parsed_cropdetect_0 @ 0x2] x1:0 x2:1919 y1:140 y2:939 w:1920 "
             "h:800 x:0 y:140 pts:19920 t:19.920000 limit:16.000000 "
             "crop=1920:800:0:140")


class ParseScanTests(unittest.TestCase):
    def test_interlaced_summary(self):
        scan = parse_scan(IDET_INTERLACED)
        self.assertEqual((scan.tff, scan.bff, scan.progressive), (250, 0, 0))
        self.assertAlmostEqual(scan.interlaced_ratio, 1.0)
        self.assertAlmostEqual(scan.repeat_ratio, 0.0)

    def test_progressive_summary(self):
        scan = parse_scan(IDET_PROGRESSIVE)
        self.assertAlmostEqual(scan.interlaced_ratio, 0.0)

    def test_telecine_shows_repeated_fields(self):
        scan = parse_scan(IDET_TELECINE)
        self.assertGreater(scan.repeat_ratio, analyze.TELECINE_RATIO)

    def test_all_zero_summary_is_ignored_in_favour_of_the_real_one(self):
        """idet 有时会打出一组全零的汇总，取帧数最多的那组。"""
        noise = ["[Parsed_idet_0 @ 0x9] Multi frame detection: TFF:     0 "
                 "BFF:     0 Progressive:     0 Undetermined:     0"]
        scan = parse_scan(noise + IDET_PROGRESSIVE)
        self.assertEqual(scan.progressive, 500)

    def test_last_crop_line_wins(self):
        early = CROP_LINE.replace("h:800", "h:900").replace(
            "crop=1920:800:0:140", "crop=1920:900:0:90")
        scan = parse_scan([early, CROP_LINE])
        self.assertEqual(scan.crop, (1920, 800, 0, 140))

    def test_nothing_parsed_is_not_a_crash(self):
        scan = parse_scan(["something unrelated", ""])
        self.assertEqual(scan.frames, 0)
        self.assertEqual(scan.crop, ())
        self.assertAlmostEqual(scan.interlaced_ratio, 0.0)


class ScanCmdTests(unittest.TestCase):
    def test_one_decode_runs_both_filters(self):
        cmd = analyze.scan_cmd("ffmpeg", Path("a.mkv"), 16, True, True)
        chain = cmd[cmd.index("-vf") + 1]
        self.assertEqual(chain, "idet,cropdetect=limit=16:round=2:reset=0")
        self.assertIn("-f", cmd)
        self.assertEqual(cmd[-2:], ["null", "-"])

    def test_reset_zero_is_mandatory(self):
        """reset=0 才是「所有帧的并集」；否则只映射最近一段，硬字幕会被裁掉。"""
        cmd = analyze.scan_cmd("ffmpeg", Path("a.mkv"), 24, False, True)
        self.assertIn("reset=0", cmd[cmd.index("-vf") + 1])
        self.assertNotIn("idet", cmd[cmd.index("-vf") + 1])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.info = make_info(width=1920, height=1080)

    def plan(self, scan, info=None):
        return make_plan(scan, info or self.info, self.cfg)

    def test_progressive_source_gets_no_filters(self):
        plan = self.plan(Scan(progressive=500, repeat_neither=500, ok=True))
        self.assertEqual(plan.filters, ())
        self.assertEqual(plan.vf, "")
        self.assertEqual(plan.skip_reason, "")
        self.assertEqual(plan.expected_size(self.info), (1920, 1080))

    def test_interlaced_source_is_deinterlaced(self):
        plan = self.plan(Scan(tff=250, repeat_neither=250, ok=True))
        self.assertEqual(len(plan.filters), 1)
        self.assertIn("bwdif=mode=send_frame", plan.filters[0])
        self.assertTrue(any("deinterlacing" in n for n in plan.notes))

    def test_send_frame_keeps_the_frame_rate(self):
        """send_field 会把帧率翻倍，时长/进度/逐帧 VMAF 全要重新对账。"""
        plan = self.plan(Scan(tff=250, repeat_neither=250, ok=True))
        self.assertNotIn("send_field", plan.vf)

    def test_deinterlace_can_be_turned_off(self):
        self.cfg["analyze"]["deinterlace"] = "off"
        plan = self.plan(Scan(tff=250, repeat_neither=250, ok=True))
        self.assertEqual(plan.filters, ())
        self.assertTrue(any("combing will be baked" in n for n in plan.notes))

    def test_mostly_progressive_is_left_alone(self):
        plan = self.plan(Scan(tff=10, progressive=490, repeat_neither=500,
                              ok=True))
        self.assertEqual(plan.filters, ())

    def test_field_order_flag_is_used_when_idet_said_nothing(self):
        info = make_info(width=720, height=576, field_order="tt")
        plan = self.plan(Scan(ok=True), info)
        self.assertIn("bwdif", plan.vf)

    def test_telecine_is_skipped(self):
        plan = self.plan(Scan(tff=400, progressive=100, repeat_neither=300,
                              repeat_top=100, repeat_bottom=100, ok=True))
        self.assertIn("telecined", plan.skip_reason)
        self.assertEqual(plan.filters, ())

    def test_telecine_skip_can_be_disabled(self):
        self.cfg["analyze"]["skip_telecine"] = False
        plan = self.plan(Scan(tff=400, progressive=100, repeat_neither=300,
                              repeat_top=100, repeat_bottom=100, ok=True))
        self.assertEqual(plan.skip_reason, "")
        self.assertIn("bwdif", plan.vf)

    def test_black_bars_are_cropped(self):
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(1920, 800, 0, 140), ok=True))
        self.assertEqual(plan.vf, "crop=1920:800:0:140")
        self.assertEqual(plan.expected_size(self.info), (1920, 800))

    def test_deinterlace_runs_before_crop(self):
        plan = self.plan(Scan(tff=250, repeat_neither=250,
                              crop=(1920, 800, 0, 140), ok=True))
        self.assertEqual(plan.filters[0].split("=")[0], "bwdif")
        self.assertTrue(plan.filters[1].startswith("crop="))

    def test_tiny_bars_are_not_worth_cropping(self):
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(1920, 1076, 0, 2), ok=True))
        self.assertEqual(plan.filters, ())

    def test_crop_can_be_turned_off(self):
        self.cfg["analyze"]["crop"] = "off"
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(1920, 800, 0, 140), ok=True))
        self.assertEqual(plan.filters, ())

    def test_absurd_crop_is_refused(self):
        """内容不足原画一半：更可能是探查被全黑片段带偏，不是真黑边。"""
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(1920, 400, 0, 340), ok=True))
        self.assertEqual(plan.filters, ())

    def test_odd_geometry_is_refused(self):
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(1920, 801, 0, 139), ok=True))
        self.assertEqual(plan.filters, ())

    def test_oversized_crop_is_refused(self):
        plan = self.plan(Scan(progressive=500, repeat_neither=500,
                              crop=(3840, 2160, 0, 0), ok=True))
        self.assertEqual(plan.filters, ())

    def test_failed_analysis_encodes_as_is(self):
        """探查失败不该顶掉编码，只是拿不到滤镜链。"""
        plan = self.plan(Scan(ok=False, error="Invalid data found"))
        self.assertEqual(plan.filters, ())
        self.assertEqual(plan.skip_reason, "")
        self.assertTrue(any("analysis failed" in n for n in plan.notes))


class AnalyseSwitchTests(unittest.TestCase):
    def test_disabled_skips_the_decode_pass(self):
        cfg = make_cfg()
        cfg["analyze"]["enabled"] = False
        called = []
        orig = analyze.run_scan
        analyze.run_scan = lambda *a, **kw: called.append(1) or Scan(ok=True)
        try:
            plan = analyze.analyse(helpers.FakeTools(), Path("a.mkv"),
                                   make_info(), cfg)
        finally:
            analyze.run_scan = orig
        self.assertEqual(called, [])
        self.assertEqual(plan.vf, "")


if __name__ == "__main__":
    unittest.main()
