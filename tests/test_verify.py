import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress import encode, verify
from vcompress.verify import check_basic, check_color


class CheckColorTests(unittest.TestCase):
    """色彩/HDR 元数据丢失是 VMAF 测不出来的「视觉有损」，必须单独拦。"""

    def setUp(self):
        self.cfg = make_cfg()

    def hdr_info(self, **kw):
        base = dict(color_primaries="bt2020", color_transfer="smpte2084",
                    color_space="bt2020nc", color_range="tv",
                    hdr_metadata=["content-light-level", "mastering-display"])
        base.update(kw)
        return make_info(**base)

    def test_identical_metadata_passes(self):
        ok, msg = check_color(self.hdr_info(), self.hdr_info(), self.cfg)
        self.assertTrue(ok, msg)

    def test_lost_transfer_fails(self):
        out = self.hdr_info(color_transfer="")
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertFalse(ok)
        self.assertIn("transfer smpte2084 -> unset", msg)

    def test_changed_primaries_fails(self):
        out = self.hdr_info(color_primaries="bt709")
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertFalse(ok)
        self.assertIn("primaries bt2020 -> bt709", msg)

    def test_lost_mastering_display_fails(self):
        out = self.hdr_info(hdr_metadata=["content-light-level"])
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertFalse(ok)
        self.assertIn("lost mastering-display", msg)

    def test_untagged_source_gaining_defaults_is_fine(self):
        """源本来没标，编码器补了默认值，不算退化。"""
        orig = make_info(color_primaries="", color_transfer="",
                         color_space="", color_range="")
        out = self.hdr_info(hdr_metadata=[])
        ok, msg = check_color(orig, out, self.cfg)
        self.assertTrue(ok, msg)

    def test_can_be_disabled(self):
        self.cfg["verify"]["color_check"] = False
        out = self.hdr_info(color_transfer="", hdr_metadata=[])
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertTrue(ok, msg)


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

    def test_dropped_audio_track_fails(self):
        orig = make_info(duration=600.0, audio_codecs=["dts", "aac"],
                         audio_bitrates=[1_500_000, 192_000],
                         audio_channels=[6, 2])
        out = make_info(video_codec="av1", duration=600.0,
                        audio_codecs=["opus"], audio_bitrates=[480_000],
                        audio_channels=[6])
        ok, msg = check_basic(orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("audio stream count changed 2 -> 1", msg)

    def test_dropped_subtitle_fails(self):
        orig = make_info(duration=600.0, subtitle_codecs=["subrip", "ass"])
        out = make_info(video_codec="av1", duration=600.0,
                        subtitle_codecs=["subrip"])
        ok, msg = check_basic(orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("subtitle stream count changed 2 -> 1", msg)

    def test_converted_streams_with_same_count_pass(self):
        orig = make_info(duration=600.0, audio_codecs=["flac"],
                         subtitle_codecs=["mov_text"])
        out = make_info(video_codec="av1", duration=600.0,
                        audio_codecs=["opus"], subtitle_codecs=["subrip"])
        ok, msg = check_basic(orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertTrue(ok, msg)

    def test_resolution_change_fails(self):
        """滤镜链写错或编码器悄悄降采样，这里必须拦住。"""
        out = make_info(video_codec="av1", duration=600.0,
                        width=1280, height=720)
        ok, msg = check_basic(self.orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("resolution changed 1920x1080 -> 1280x720", msg)

    def test_unknown_resolution_is_not_a_failure(self):
        out = make_info(video_codec="av1", duration=600.0, width=0, height=0)
        ok, msg = check_basic(self.orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertTrue(ok, msg)


class RunAllTests(unittest.TestCase):
    """校验链的编排：便宜的检查先跑，环境缺能力只告警，画质不过才算失败。"""

    def setUp(self):
        self.cfg = make_cfg()
        self.orig = make_info(duration=600.0, size=100_000)
        self.out_info = make_info(video_codec="av1", duration=600.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "a.tmp.mkv"
        self.out.write_bytes(b"x" * 40_000)
        self.calls = []
        self.decode = None
        self.spot = (96.0, "")
        self.orig_decode = verify.decode_errors
        self.orig_spot = verify.vmaf_spot_check
        verify.decode_errors = self.fake_decode
        verify.vmaf_spot_check = self.fake_spot

    def tearDown(self):
        verify.decode_errors = self.orig_decode
        verify.vmaf_spot_check = self.orig_spot
        self.tmp.cleanup()

    def fake_decode(self, ffmpeg, path):
        self.calls.append("decode")
        return self.decode

    def fake_spot(self, *a, **kw):
        self.calls.append("vmaf")
        return self.spot

    def run_all(self):
        return verify.run_all(FakeTools(), Path("src.mp4"), self.out,
                              self.orig, self.out_info, self.cfg)

    def test_happy_path_returns_the_score(self):
        self.assertAlmostEqual(self.run_all(), 96.0)
        self.assertEqual(self.calls, ["decode", "vmaf"])

    def test_decode_failure_stops_before_the_expensive_check(self):
        self.decode = ["Invalid data found"]
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("decode check failed", str(cm.exception))
        self.assertEqual(self.calls, ["decode"], "vmaf must not have run")

    def test_decode_check_can_be_disabled(self):
        self.cfg["verify"]["decode_check"] = False
        self.run_all()
        self.assertEqual(self.calls, ["vmaf"])

    def test_basic_failure_stops_before_vmaf(self):
        self.out_info.duration = 700.0
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("duration mismatch", str(cm.exception))
        self.assertEqual(self.calls, ["decode"])

    def test_color_metadata_loss_stops_before_vmaf(self):
        self.orig.color_transfer = "smpte2084"
        self.out_info.color_transfer = ""
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("color metadata changed", str(cm.exception))
        self.assertEqual(self.calls, ["decode"], "vmaf must not have run")

    def test_low_vmaf_is_a_failure(self):
        self.spot = (70.0, "vmaf mean 70.00 below threshold 93.0")
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("below threshold", str(cm.exception))

    def test_unavailable_vmaf_only_warns(self):
        self.spot = (None, "libvmaf failed: no such filter")
        self.assertIsNone(self.run_all())

    def test_disabled_vmaf_passes(self):
        self.spot = (None, "")
        self.assertIsNone(self.run_all())


class VmafModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_4k_source_gets_the_4k_model(self):
        info = make_info(width=3840, height=2160)
        self.assertEqual(verify.vmaf_model(info, self.cfg),
                         encode.VMAF_MODEL_4K)

    def test_1080p_source_gets_the_default(self):
        self.assertEqual(verify.vmaf_model(make_info(), self.cfg), "")

    def test_switch_can_be_turned_off(self):
        self.cfg["verify"]["vmaf_4k_model"] = False
        info = make_info(width=3840, height=2160)
        self.assertEqual(verify.vmaf_model(info, self.cfg), "")


class VmafSpotCheckTests(unittest.TestCase):
    """用替身挡掉 ffmpeg，只验证参数处理与结果判定。"""

    def setUp(self):
        self.cfg = make_cfg()
        self.cfg["verify"]["vmaf_segments"] = 2
        self.cfg["verify"]["vmaf_sample_seconds"] = 5
        self.extracted = []
        self.scores = [96.0, 94.0]
        self.shifts = []
        self.orig_extract = encode.extract_segment
        self.orig_score = encode.vmaf_score
        encode.extract_segment = lambda ff, src, start, secs, out, codec="libx264": \
            self.extracted.append((Path(src).name, round(start, 3)))
        encode.vmaf_score = self.fake_score

    def fake_score(self, ff, dist, ref, shift=0, **kw):
        self.shifts.append(shift)
        return self.scores[len(self.extracted) // 2 - 1]

    def tearDown(self):
        encode.extract_segment = self.orig_extract
        encode.vmaf_score = self.orig_score

    def check(self, duration=600.0, fps=0.0):
        return verify.vmaf_spot_check(FakeTools(), Path("a.mkv"),
                                      Path("b.mkv"), duration, self.cfg,
                                      fps=fps)

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
        self.cfg["verify"]["vmaf_min_segment"] = 0.0
        self.scores = [80.0, 82.0]
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 81.0)
        self.assertIn("below threshold", msg)
        self.assertIn("80.00", msg)
        self.assertIn("82.00", msg)

    def test_one_bad_segment_fails_even_when_the_mean_passes(self):
        """99/99/81 的均值是 93，够过阈值，但那一段是看得出来的。"""
        self.cfg["verify"]["vmaf_segments"] = 3
        self.scores = [99.0, 99.0, 81.0]
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 93.0)
        self.assertIn("per-segment floor", msg)
        self.assertIn("81.00", msg)

    def test_per_segment_floor_can_be_disabled(self):
        self.cfg["verify"]["vmaf_segments"] = 3
        self.cfg["verify"]["vmaf_min_segment"] = 0.0
        self.scores = [99.0, 99.0, 81.0]
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 93.0)
        self.assertEqual(msg, "")

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
        def boom(*a, **kw):
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

    def test_reference_and_distorted_share_one_cut_point(self):
        self.check(fps=24000 / 1001)
        starts = [s for _n, s in self.extracted]
        self.assertEqual(starts[0], starts[1], "ref/dist must be cut alike")
        self.assertEqual(starts[2], starts[3])

    def test_cut_points_land_between_frames(self):
        """切点必须落在帧间隙里，否则容器时间戳取整会让两边错开一帧。"""
        fps = 24000 / 1001
        self.check(fps=fps)
        for _name, start in self.extracted:
            offset = (start * fps) % 1.0
            self.assertAlmostEqual(offset, 0.5, places=2,
                                   msg=f"{start}s sits on a frame boundary")

    def test_low_segment_is_rechecked_for_a_frame_offset(self):
        """错帧量出来的低分不该判死刑：挪一帧还原后按真实分数算。"""
        def score(ff, dist, ref, shift=0, **kw):
            self.shifts.append(shift)
            return 95.0 if shift else 40.0

        encode.vmaf_score = score
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 95.0)
        self.assertEqual(msg, "")
        self.assertIn(1, self.shifts)

    def test_genuinely_bad_segment_still_fails(self):
        """挪帧也救不回来的低分说明画质真的坏了，必须照旧判失败。"""
        encode.vmaf_score = lambda ff, dist, ref, shift=0, **kw: 70.0
        mean, msg = self.check()
        self.assertAlmostEqual(mean, 70.0)
        self.assertIn("70.00", msg)

    def test_good_scores_are_not_rechecked(self):
        self.check()
        self.assertEqual(self.shifts, [0, 0], "no extra vmaf runs when passing")

    def test_failure_message_says_where_it_measured(self):
        self.scores = [80.0, 82.0]
        _mean, msg = self.check(duration=600.0)
        self.assertRegex(msg, r"80\.00@\d+s")
        self.assertRegex(msg, r"82\.00@\d+s")


class FakeTools:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"


if __name__ == "__main__":
    unittest.main()

