import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from collections import Counter
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress import verify, vmaf
from vcompress.verify import check_basic, check_color, count_decode_errors


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
        self.assertIn("transfer smpte2084 -> 未标注", msg)

    def test_changed_primaries_fails(self):
        out = self.hdr_info(color_primaries="bt709")
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertFalse(ok)
        self.assertIn("primaries bt2020 -> bt709", msg)

    def test_lost_mastering_display_fails(self):
        out = self.hdr_info(hdr_metadata=["content-light-level"])
        ok, msg = check_color(self.hdr_info(), out, self.cfg)
        self.assertFalse(ok)
        self.assertIn("丢了 mastering-display", msg)

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
        self.assertIn("时长", msg)

    def test_missing_video_stream_fails(self):
        out = make_info(video_codec="", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100, 50, self.cfg)
        self.assertFalse(ok)
        self.assertIn("没有视频流", msg)

    def test_output_not_smaller_fails(self):
        out = make_info(video_codec="av1", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100, 101, self.cfg)
        self.assertFalse(ok)
        self.assertIn("没比源文件", msg)

    def test_tiny_saving_discarded(self):
        out = make_info(video_codec="av1", duration=600.0)
        ok, msg = check_basic(self.orig, out, 100_000_000, 98_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("丢弃产物", msg)

    def test_dropped_audio_track_fails(self):
        orig = make_info(duration=600.0, audio_codecs=["dts", "aac"],
                         audio_bitrates=[1_500_000, 192_000],
                         audio_channels=[6, 2])
        out = make_info(video_codec="av1", duration=600.0,
                        audio_codecs=["opus"], audio_bitrates=[480_000],
                        audio_channels=[6])
        ok, msg = check_basic(orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("音轨条数变了：2 -> 1", msg)

    def test_dropped_subtitle_fails(self):
        orig = make_info(duration=600.0, subtitle_codecs=["subrip", "ass"])
        out = make_info(video_codec="av1", duration=600.0,
                        subtitle_codecs=["subrip"])
        ok, msg = check_basic(orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertFalse(ok)
        self.assertIn("字幕条数变了：2 -> 1", msg)

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
        self.assertIn("分辨率变了：1920x1080 -> 1280x720", msg)

    def test_unknown_resolution_is_not_a_failure(self):
        out = make_info(video_codec="av1", duration=600.0, width=0, height=0)
        ok, msg = check_basic(self.orig, out, 100_000_000, 40_000_000, self.cfg)
        self.assertTrue(ok, msg)


class DecodeErrorCountTests(unittest.TestCase):
    """错误行归一化：地址和"重复 N 次"折叠必须先摊平，否则源和产物对不上。"""

    def test_object_addresses_are_stripped(self):
        a = count_decode_errors(["[mp3float @ 0x5f344f064d80] Header missing"])
        b = count_decode_errors(["[mp3float @ 0x62296f83db00] Header missing"])
        self.assertEqual(a, b)

    def test_stream_indexes_are_stripped(self):
        """同一条音轨在 AVI 里是 0:1，在产物里可能是 0:0。"""
        a = count_decode_errors(["[aist#0:1/mp3 @ 0x1] [dec:mp3float @ 0x2] "
                                 "Error submitting packet to decoder"])
        b = count_decode_errors(["[aist#0:0/mp3 @ 0x3] [dec:mp3float @ 0x4] "
                                 "Error submitting packet to decoder"])
        self.assertEqual(a, b)

    def test_different_decoders_stay_different(self):
        """抹序号不能抹掉「哪个解码器报的」，视频和音频的错不能混为一谈。"""
        a = count_decode_errors(["[vist#0:0/mpeg4 @ 0x1] Error submitting"])
        b = count_decode_errors(["[aist#0:0/mp3 @ 0x1] Error submitting"])
        self.assertNotEqual(a, b)

    def test_repeat_line_expands_the_previous_message(self):
        counts = count_decode_errors([
            "[mp3float @ 0x1] Header missing",
            "    Last message repeated 3 times",
        ])
        self.assertEqual(list(counts.values()), [4])

    def test_repeat_singular_form(self):
        counts = count_decode_errors([
            "[mp3float @ 0x1] Header missing",
            "    Last message repeated 1 times",
            "[mp3float @ 0x2] Header missing",
        ])
        self.assertEqual(list(counts.values()), [3])

    def test_blank_lines_and_none_are_ignored(self):
        self.assertEqual(count_decode_errors(None), Counter())
        self.assertEqual(count_decode_errors(["", "  "]), Counter())


class AddedDecodeErrorTests(unittest.TestCase):
    """解码校验的源文件基线：片源自带的坏帧不算产物的错。

    真实案例：一个 mpeg4+mp3 的 AVI 片源，mp3 轨有 4 个坏帧，音轨 copy 后
    原样进了产物，结果三个多小时的正常编码被判「解码校验没过」，而且每次
    重跑都在同一处翻车。
    """

    def setUp(self):
        self.calls = []
        self.errs = {}
        self.orig_decode = verify.decode_errors
        verify.decode_errors = self.fake_decode

    def tearDown(self):
        verify.decode_errors = self.orig_decode

    def fake_decode(self, ffmpeg, path):
        self.calls.append(Path(path).name)
        return self.errs.get(Path(path).name)

    def added(self):
        return verify.added_decode_errors("ffmpeg", Path("a.tmp.mkv"),
                                          Path("a.avi"))

    def test_clean_output_never_touches_the_source(self):
        self.assertEqual(self.added(), [])
        self.assertEqual(self.calls, ["a.tmp.mkv"],
                         "产物解码干净时不该再解一遍源文件")

    def test_errors_inherited_from_the_source_pass(self):
        """取自真实片源的错误文本：音轨序号从 0:1 变成 0:0 也算同一条。"""
        self.errs["a.tmp.mkv"] = [
            "[mp3float @ 0xaaa] Header missing",
            "    Last message repeated 1 times",
            "[mp3float @ 0xbbb] Header missing",
            "[aist#0:0/mp3 @ 0xccc] [dec:mp3float @ 0xddd] Error submitting "
            "packet to decoder: Invalid data found when processing input",
        ]
        self.errs["a.avi"] = [
            "[mp3float @ 0x111] Header missing",
            "    Last message repeated 1 times",
            "[mp3float @ 0x222] Header missing",
            "[aist#0:1/mp3 @ 0x333] [dec:mp3float @ 0x444] Error submitting "
            "packet to decoder: Invalid data found when processing input",
            "[mpeg4 @ 0x555] low_delay flag set incorrectly, clearing it",
        ]
        self.assertEqual(self.added(), [])
        self.assertEqual(self.calls, ["a.tmp.mkv", "a.avi"])

    def test_new_error_in_the_output_still_fails(self):
        self.errs["a.tmp.mkv"] = [
            "[mp3float @ 0xaaa] Header missing",
            "[matroska @ 0xbbb] Invalid data found when processing input",
        ]
        self.errs["a.avi"] = ["[mp3float @ 0xccc] Header missing"]
        added = self.added()
        self.assertEqual(len(added), 1)
        self.assertIn("Invalid data found", added[0])

    def test_more_occurrences_than_the_source_fails(self):
        """同一条消息在产物里多出来，也是产物的问题。"""
        self.errs["a.tmp.mkv"] = [
            "[mp3float @ 0xaaa] Header missing",
            "    Last message repeated 9 times",
        ]
        self.errs["a.avi"] = ["[mp3float @ 0xccc] Header missing"]
        added = self.added()
        self.assertEqual(len(added), 1)
        self.assertIn("产物 10 次，源文件只有 1 次", added[0])

    def test_repeated_new_error_reports_its_count(self):
        self.errs["a.tmp.mkv"] = [
            "[av1 @ 0xaaa] Bad frame",
            "    Last message repeated 4 times",
        ]
        added = self.added()
        self.assertEqual(len(added), 1)
        self.assertIn("（5 次）", added[0])

    def test_source_noise_absent_from_the_output_is_not_a_failure(self):
        """比对是单向的：源里多出来的行不该反过来算问题。"""
        self.errs["a.tmp.mkv"] = ["[mp3float @ 0xaaa] Header missing"]
        self.errs["a.avi"] = [
            "[mp3float @ 0xccc] Header missing",
            "[null @ 0xddd] non monotonically increasing dts",
        ]
        self.assertEqual(self.added(), [])


class RunAllTests(unittest.TestCase):
    """校验链的编排：按代价从便宜到贵，环境缺能力只告警，画质不过才算失败。"""

    def setUp(self):
        self.cfg = make_cfg()
        self.orig = make_info(duration=600.0, size=100_000)
        self.out_info = make_info(video_codec="av1", duration=600.0)
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "a.tmp.mkv"
        self.out.write_bytes(b"x" * 40_000)
        self.calls = []
        self.decode = None
        self.src_decode = None
        self.spot = (96.0, 95.0, "")
        self.orig_decode = verify.decode_errors
        self.orig_spot = verify.vmaf_spot_check
        verify.decode_errors = self.fake_decode
        verify.vmaf_spot_check = self.fake_spot

    def tearDown(self):
        verify.decode_errors = self.orig_decode
        verify.vmaf_spot_check = self.orig_spot
        self.tmp.cleanup()

    def fake_decode(self, ffmpeg, path):
        """产物和源文件的解码分别记账：基线是惰性的，得看出它有没有被跑到。"""
        out = Path(path) == self.out
        self.calls.append("decode" if out else "baseline")
        return self.decode if out else self.src_decode

    def fake_spot(self, *a, **kw):
        self.calls.append("vmaf")
        return self.spot

    def run_all(self):
        return verify.run_all(FakeTools(), Path("src.mp4"), self.out,
                              self.orig, self.out_info, self.cfg)

    def test_happy_path_returns_the_score(self):
        mean, worst = self.run_all()
        self.assertAlmostEqual(mean, 96.0)
        self.assertAlmostEqual(worst, 95.0)
        self.assertEqual(self.calls, ["decode", "vmaf"])

    def test_decode_failure_stops_before_the_expensive_check(self):
        self.decode = ["Invalid data found"]
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("解码校验没过", str(cm.exception))
        self.assertEqual(self.calls, ["decode", "baseline"],
                         "vmaf must not have run")

    def test_source_inherited_decode_errors_do_not_fail_the_run(self):
        self.decode = ["[mp3float @ 0xaaa] Header missing"]
        self.src_decode = ["[mp3float @ 0xbbb] Header missing"]
        mean, worst = self.run_all()
        self.assertAlmostEqual(mean, 96.0)
        self.assertEqual(self.calls, ["decode", "baseline", "vmaf"])

    def test_decode_check_can_be_disabled(self):
        self.cfg["verify"]["decode_check"] = False
        self.run_all()
        self.assertEqual(self.calls, ["vmaf"])

    def test_basic_failure_stops_before_vmaf(self):
        self.out_info.duration = 700.0
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("时长差了", str(cm.exception))
        self.assertEqual(self.calls, [], "元数据就能判的事，不该先解码")

    def test_bigger_output_is_judged_without_decoding(self):
        """产物比源文件大：这是 stat() 就能下的结论，别先白解一遍全片。"""
        self.out.write_bytes(b"x" * 200_000)
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("没比源文件", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_color_metadata_loss_stops_before_vmaf(self):
        self.orig.color_transfer = "smpte2084"
        self.out_info.color_transfer = ""
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("色彩/HDR 元数据变了", str(cm.exception))
        self.assertEqual(self.calls, [], "元数据就能判的事，不该先解码")

    def test_low_vmaf_is_a_failure(self):
        self.spot = (70.0, 68.0, "vmaf mean 70.00 below threshold 93.0")
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("below threshold", str(cm.exception))

    def test_measurement_failure_is_a_hard_failure(self):
        """有 libvmaf 却量不出这个文件——不能说通过，原文件可能跟着被删。"""
        self.spot = (None, None, "抽取样本片段失败：No space left on device")
        with self.assertRaises(verify.VerifyError) as cm:
            self.run_all()
        self.assertIn("量不出 VMAF", str(cm.exception))
        self.assertIn("No space left", str(cm.exception))

    def test_build_without_libvmaf_only_warns(self):
        """整个构建都没有 libvmaf：硬失败等于这台机器上没法用，降级告警。"""
        self.spot = (None, None, "libvmaf failed: no such filter")
        self.assertEqual(
            verify.run_all(FakeTools(), Path("src.mp4"), self.out, self.orig,
                           self.out_info, self.cfg, vmaf_available=False),
            (None, None))

    def test_disabled_vmaf_passes(self):
        self.spot = (None, None, "")
        self.assertEqual(self.run_all(), (None, None))


class VmafModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_4k_source_gets_the_4k_model(self):
        info = make_info(width=3840, height=2160)
        self.assertEqual(verify.vmaf_model(info, self.cfg),
                         vmaf.MODEL_4K)

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
        self.orig_extract = vmaf.extract_segment
        self.orig_score = vmaf.measure
        vmaf.extract_segment = lambda ff, src, start, secs, out, codec="libx264": \
            self.extracted.append((Path(src).name, round(start, 3)))
        vmaf.measure = self.fake_score

    def fake_score(self, ff, dist, ref, shift=0, **kw):
        self.shifts.append(shift)
        return self.scores[len(self.extracted) // 2 - 1]

    def tearDown(self):
        vmaf.extract_segment = self.orig_extract
        vmaf.measure = self.orig_score

    def check(self, duration=600.0, fps=0.0):
        return verify.vmaf_spot_check(FakeTools(), Path("a.mkv"),
                                      Path("b.mkv"), duration, self.cfg,
                                      fps=fps)

    def check_mean_msg(self, duration=600.0, fps=0.0):
        mean, _worst, msg = self.check(duration, fps)
        return mean, msg

    def test_disabled_is_not_reported_as_unavailable(self):
        self.cfg["verify"]["vmaf_enabled"] = False
        self.assertEqual(self.check(), (None, None, ""))
        self.assertEqual(self.extracted, [])

    def test_mean_above_threshold_passes(self):
        mean, worst, msg = self.check()
        self.assertAlmostEqual(mean, 95.0)
        self.assertAlmostEqual(worst, 94.0)
        self.assertEqual(msg, "")
        self.assertEqual(len(self.extracted), 4, "2 segments x ref+dist")

    def test_mean_below_threshold_reports_every_segment(self):
        self.cfg["verify"]["vmaf_min_segment"] = 0.0
        self.scores = [80.0, 82.0]
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 81.0)
        self.assertIn("低于阈值", msg)
        self.assertIn("80.00", msg)
        self.assertIn("82.00", msg)

    def test_one_bad_segment_fails_even_when_the_mean_passes(self):
        """99/99/81 的均值是 93，够过阈值，但那一段是看得出来的。"""
        self.cfg["verify"]["vmaf_segments"] = 3
        self.scores = [99.0, 99.0, 81.0]
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 93.0)
        self.assertIn("低于单段下限", msg)
        self.assertIn("81.00", msg)

    def test_per_segment_floor_can_be_disabled(self):
        self.cfg["verify"]["vmaf_segments"] = 3
        self.cfg["verify"]["vmaf_min_segment"] = 0.0
        self.scores = [99.0, 99.0, 81.0]
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 93.0)
        self.assertEqual(msg, "")

    def test_zero_segments_still_measures_once(self):
        self.cfg["verify"]["vmaf_segments"] = 0
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 96.0)
        self.assertEqual(msg, "")

    def test_garbage_segment_count_falls_back_to_default(self):
        self.cfg["verify"]["vmaf_segments"] = "three"
        self.scores = [96.0, 96.0, 96.0]
        mean, _ = self.check_mean_msg()
        self.assertEqual(len(self.extracted), 6)
        self.assertAlmostEqual(mean, 96.0)

    def test_unknown_duration_is_reported(self):
        mean, msg = self.check_mean_msg(duration=0.0)
        self.assertIsNone(mean)
        self.assertIn("时长", msg)
        self.assertEqual(self.extracted, [])

    def test_ffmpeg_failure_is_surfaced_not_raised(self):
        def boom(*a, **kw):
            raise vmaf.VmafError("libvmaf failed: no such filter")

        vmaf.measure = boom
        mean, msg = self.check_mean_msg()
        self.assertIsNone(mean)
        self.assertIn("no such filter", msg)

    def test_sample_never_starts_past_the_end(self):
        self.check(duration=8.0)
        for _name, start in self.extracted:
            self.assertGreaterEqual(start, 0.0)
            self.assertLessEqual(start, 8.0)

    def test_short_file_is_not_measured_five_times(self):
        """片子比样本还短时几段会挤到同一个切点，量一次就够。"""
        self.cfg["verify"]["vmaf_segments"] = 5
        self.cfg["verify"]["vmaf_sample_seconds"] = 20
        mean, msg = self.check_mean_msg(duration=10.0)
        self.assertEqual(msg, "")
        self.assertAlmostEqual(mean, 96.0)
        self.assertEqual(len(self.extracted), 2,
                         "1 segment x ref+dist, not 5 copies of the same one")

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

        vmaf.measure = score
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 95.0)
        self.assertEqual(msg, "")
        self.assertIn(1, self.shifts)

    def test_genuinely_bad_segment_still_fails(self):
        """挪帧也救不回来的低分说明画质真的坏了，必须照旧判失败。"""
        vmaf.measure = lambda ff, dist, ref, shift=0, **kw: 70.0
        mean, msg = self.check_mean_msg()
        self.assertAlmostEqual(mean, 70.0)
        self.assertIn("70.00", msg)

    def test_good_scores_are_not_rechecked(self):
        self.check()
        self.assertEqual(self.shifts, [0, 0], "no extra vmaf runs when passing")

    def test_failure_message_says_where_it_measured(self):
        self.scores = [80.0, 82.0]
        _mean, msg = self.check_mean_msg(duration=600.0)
        self.assertRegex(msg, r"80\.00@\d+s")
        self.assertRegex(msg, r"82\.00@\d+s")


class FakeTools:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"


if __name__ == "__main__":
    unittest.main()

