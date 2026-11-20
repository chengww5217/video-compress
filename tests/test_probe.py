import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import unittest
from unittest import mock

import helpers
from helpers import make_cfg, make_info
from vcompress import probe
from vcompress.probe import (_color_tag, _detect_vfr, _hdr_markers,
                             _parse_fps, _rate_plan_from_durations,
                             _rate_string, _rotation, _stream_bitrate,
                             should_skip)
from vcompress.util import to_int as _to_int


class ColorMetadataTests(unittest.TestCase):
    def test_color_tag_normalises(self):
        self.assertEqual(_color_tag("BT2020"), "bt2020")
        self.assertEqual(_color_tag(" smpte2084 "), "smpte2084")

    def test_color_tag_treats_unknown_as_missing(self):
        for value in ("unknown", "unspecified", "reserved", "N/A", "", None):
            self.assertEqual(_color_tag(value), "", repr(value))

    def test_hdr_markers_from_stream_and_frame(self):
        stream = [{"side_data_type": "Content light level metadata"}]
        frame = [{"side_data_type": "Mastering display metadata"},
                 {"side_data_type": "H.26[45] User Data Unregistered SEI message"}]
        self.assertEqual(_hdr_markers(stream, frame),
                         ["content-light-level", "mastering-display"])

    def test_hdr_markers_deduplicates(self):
        entry = [{"side_data_type": "Mastering display metadata"}]
        self.assertEqual(_hdr_markers(entry, entry), ["mastering-display"])

    def test_hdr_markers_tolerates_junk(self):
        self.assertEqual(_hdr_markers(None, [], ["not-a-dict"], [{}]), [])

    def test_dolby_vision_recognised(self):
        self.assertEqual(_hdr_markers([{"side_data_type": "Dolby Vision RPU Data"}]),
                         ["dolby-vision"])


class RotationTests(unittest.TestCase):
    """旋转过的片源，编码尺寸和播放器看到的尺寸是反的。"""

    def test_display_matrix_rotation(self):
        stream = {"side_data_list": [{"side_data_type": "Display Matrix",
                                      "rotation": 90}]}
        self.assertEqual(_rotation(stream), 90)

    def test_negative_rotation_is_normalised(self):
        stream = {"side_data_list": [{"rotation": -90}]}
        self.assertEqual(_rotation(stream), 270)

    def test_rotation_from_legacy_tag(self):
        self.assertEqual(_rotation({"tags": {"rotate": "270"}}), 270)

    def test_rotation_zero_when_missing(self):
        self.assertEqual(_rotation({}), 0)
        self.assertEqual(_rotation({"side_data_list": [{}]}), 0)

    def test_display_dimensions_swap_for_quarter_turns(self):
        info = make_info(width=1088, height=1920, rotation=90)
        self.assertEqual((info.display_width, info.display_height),
                         (1920, 1088))
        info = make_info(width=1088, height=1920, rotation=180)
        self.assertEqual((info.display_width, info.display_height),
                         (1088, 1920))


class PrimaryVideoStreamTests(unittest.TestCase):
    """probe 必须和 -map 0:V:0 取同一条流：封面图（attached_pic）不算正片。"""

    def test_attached_picture_is_skipped(self):
        streams = [
            {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 0}},
        ]
        self.assertEqual(probe.primary_video_stream(streams)["index"], 1)

    def test_first_real_video_is_used(self):
        streams = [
            {"index": 0, "codec_type": "audio"},
            {"index": 1, "codec_type": "video", "disposition": {}},
            {"index": 2, "codec_type": "video", "disposition": {}},
        ]
        self.assertEqual(probe.primary_video_stream(streams)["index"], 1)

    def test_no_real_video_returns_empty(self):
        streams = [
            {"index": 0, "codec_type": "video", "disposition": {"attached_pic": 1}},
        ]
        self.assertEqual(probe.primary_video_stream(streams), {})
        self.assertEqual(probe.primary_video_stream([]), {})


class ParseHelperTests(unittest.TestCase):
    def test_parse_fps_fraction(self):
        self.assertAlmostEqual(_parse_fps("30000/1001"), 29.97, places=2)

    def test_parse_fps_plain_number(self):
        self.assertEqual(_parse_fps("30"), 30.0)
        self.assertEqual(_parse_fps("24.75"), 24.75)

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


class VideoBitrateTests(unittest.TestCase):
    def test_stream_bitrate_preferred(self):
        info = make_info(bitrate=6_000_000, container_bitrate=9_000_000)
        self.assertEqual(info.video_bitrate(), 6_000_000)

    def test_container_bitrate_minus_audio(self):
        info = make_info(bitrate=0, container_bitrate=9_000_000,
                         audio_bitrates=[640_000, 192_000])
        self.assertEqual(info.video_bitrate(), 9_000_000 - 832_000)

    def test_container_only(self):
        info = make_info(bitrate=0, container_bitrate=5_000_000,
                         audio_codecs=[], audio_bitrates=[])
        self.assertEqual(info.video_bitrate(), 5_000_000)

    def test_absurd_audio_bitrate_falls_back_to_container(self):
        info = make_info(bitrate=0, container_bitrate=1_000_000,
                         audio_bitrates=[4_000_000])
        self.assertEqual(info.video_bitrate(), 1_000_000)

    def test_nothing_known(self):
        info = make_info(bitrate=0, container_bitrate=0)
        self.assertEqual(info.video_bitrate(), 0)


class StreamBitrateTests(unittest.TestCase):
    def test_bit_rate_field(self):
        self.assertEqual(_stream_bitrate({"bit_rate": "5000000"}), 5_000_000)

    def test_matroska_bps_tag(self):
        self.assertEqual(_stream_bitrate({"tags": {"BPS": "4200000"}}), 4_200_000)

    def test_language_suffixed_bps_tag(self):
        self.assertEqual(_stream_bitrate({"tags": {"BPS-eng": "3000000"}}),
                         3_000_000)

    def test_no_bitrate_info(self):
        self.assertEqual(_stream_bitrate({"tags": {"DURATION": "00:01:00"}}), 0)


class ShouldSkipTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_tagged_file_skips(self):
        skip, reason = should_skip(make_info(comment="vcompress-v1"), self.cfg)
        self.assertTrue(skip)
        self.assertIn("之前压过了", reason)

    def test_marker_match_is_case_insensitive(self):
        skip, _ = should_skip(make_info(comment="Encoded by VCompress-v1"),
                              self.cfg)
        self.assertTrue(skip)

    def test_custom_marker_is_honoured(self):
        self.cfg["skip"]["tag_marker"] = "zzmark"
        skip, reason = should_skip(make_info(comment="zzmark-v1"), self.cfg)
        self.assertTrue(skip)
        self.assertIn("zzmark", reason)

    def test_custom_marker_ignores_the_default_one(self):
        self.cfg["skip"]["tag_marker"] = "zzmark"
        skip, _ = should_skip(make_info(comment="vcompress-v1"), self.cfg)
        self.assertFalse(skip, "only the configured marker should count")

    def test_unrelated_comment_does_not_skip(self):
        skip, _ = should_skip(make_info(comment="ripped by someone"), self.cfg)
        self.assertFalse(skip)

    def test_small_file_skips(self):
        skip, reason = should_skip(make_info(size=1024), self.cfg)
        self.assertTrue(skip)
        self.assertIn("文件太小", reason)

    def test_bpp_below_threshold_skips(self):
        info = make_info(bitrate=3_000_000)
        bpp = 3_000_000 / (1920 * 1080 * 30)
        self.assertLess(bpp, 0.05)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)
        self.assertIn("低于阈值", reason)

    def test_healthy_h264_not_skipped(self):
        skip, reason = should_skip(make_info(), self.cfg)
        self.assertFalse(skip, reason)

    def test_bpp_gate_can_be_skipped_for_pilot(self):
        """pilot 模式下要能只看其余条件，不被 BPP 提前拦住。"""
        skip, reason = should_skip(make_info(bitrate=3_000_000), self.cfg,
                                   check_bpp=False)
        self.assertFalse(skip, reason)

    def test_low_bitrate_hevc_skips(self):
        info = make_info(video_codec="hevc", codec_family="hevc", bitrate=1_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)

    def test_av1_source_skips_on_predicted_saving(self):
        info = make_info(video_codec="av01", codec_family="av1", bitrate=20_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)
        self.assertIn("预估只能省", reason)

    def test_unknown_codec_not_skipped(self):
        info = make_info(video_codec="prores", codec_family="", bitrate=100_000_000)
        skip, reason = should_skip(info, self.cfg)
        self.assertFalse(skip, reason)

    def test_unknown_bitrate_skips_bpp_gate_only(self):
        info = make_info(bitrate=0)
        skip, reason = should_skip(info, self.cfg)
        self.assertFalse(skip, reason)

    def test_fat_audio_track_does_not_hide_a_thin_video_track(self):
        # 容器总码率 3.6M 看着够高，但其中 1.5M 是音轨，视频其实已压到位
        info = make_info(bitrate=0, container_bitrate=3_600_000,
                         audio_codecs=["truehd"], audio_bitrates=[1_500_000])
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip, "bpp gate must judge the video track alone")
        self.assertIn("每像素码率", reason)


class InterlacedTests(unittest.TestCase):
    """交错源必须跳过：压过去会丢交错标记，而这是 VMAF 测不出来的视觉有损。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_interlaced_source_skips(self):
        for order in ("tt", "bb", "tb", "bt"):
            skip, reason = should_skip(make_info(field_order=order), self.cfg)
            self.assertTrue(skip, order)
            self.assertIn("交错源", reason)
            self.assertIn(order, reason)

    def test_progressive_source_is_processed(self):
        for order in ("progressive", "unknown", ""):
            skip, reason = should_skip(make_info(field_order=order), self.cfg)
            self.assertFalse(skip, f"{order!r}: {reason}")

    def test_interlace_check_can_be_turned_off(self):
        self.cfg["skip"]["skip_interlaced"] = False
        skip, reason = should_skip(make_info(field_order="tt"), self.cfg)
        self.assertFalse(skip, reason)

    def test_the_gate_runs_before_the_expensive_ones(self):
        """交错的判断不该被别的跳过原因掩盖，它的理由信息量最大。"""
        info = make_info(field_order="tt", bitrate=1_000)
        _skip, reason = should_skip(info, self.cfg)
        self.assertIn("交错源", reason)

    def test_interlaced_property(self):
        self.assertTrue(make_info(field_order="bt").interlaced)
        self.assertFalse(make_info(field_order="progressive").interlaced)
        # 大小写在探测时就规范化了，这里只认小写
        self.assertFalse(make_info(field_order="BT").interlaced)


class VariableFrameRateTests(unittest.TestCase):
    """混合帧率：按帧数分位选目标；少量高帧可丢，占多数的帧率不能丢。"""

    def test_detects_mixed_frame_rate(self):
        self.assertTrue(_detect_vfr(99 / 4, 2477295 / 88943))
        self.assertTrue(_detect_vfr(24.0, 30.0))

    def test_cfr_rounding_noise_is_not_vfr(self):
        # 29.97 被写成 30、mkv 的毫秒时间基，都不是混合帧率
        self.assertFalse(_detect_vfr(30.0, 29.97))
        self.assertFalse(_detect_vfr(24000 / 1001, 24000 / 1001))
        self.assertFalse(_detect_vfr(0.0, 30.0))
        self.assertFalse(_detect_vfr(30.0, 0.0))

    def test_dominant_rate_is_kept(self):
        """30fps 占多数时不能降到标称的 24.75，否则大段丢帧。"""
        durations = ["0.040404"] * 30 + ["0.033333"] * 70
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 30.0, places=2)
        self.assertAlmostEqual(highest, 30.0, places=2)
        self.assertEqual(dropped, 0.0)

    def test_millisecond_timebase_rounding_does_not_raise_the_target(self):
        """1/1000 时间基把 30fps 写成 33/34ms，不能因此选出 30.303。"""
        durations = (["0.040"] * 95 + ["0.041"] * 54
                     + ["0.033"] * 120 + ["0.034"] * 60)
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 30.0, places=2)
        self.assertAlmostEqual(highest, 30.0, places=2)
        self.assertEqual(dropped, 0.0)

    def test_zero_budget_does_not_snap_a_single_quantized_bucket(self):
        """只有一个 33ms 桶时可能是真的 30.303fps；不能为了好看压到 30。"""
        target, highest, dropped = _rate_plan_from_durations(
            ["0.033"] * 100, 0.0)
        self.assertAlmostEqual(target, 30.303, places=2)
        self.assertAlmostEqual(highest, 30.303, places=2)
        self.assertEqual(dropped, 0.0)

    def test_zero_budget_keeps_a_quantized_pair_together(self):
        """33/34ms 是同一帧率被毫秒时间基拆开的两半，零预算也不能拆散。"""
        target, _highest, dropped = _rate_plan_from_durations(
            ["0.033"] * 120 + ["0.034"] * 60, 0.0)
        self.assertAlmostEqual(target, 30.0, places=2)
        self.assertEqual(dropped, 0.0)

    def test_quantized_low_rate_stays_at_24_75(self):
        """24.75fps 被写成 40/41ms 时，簇均值仍应归一化回 99/4。"""
        durations = (["0.040"] * 95 + ["0.041"] * 54
                     + ["0.016"] * 3 + ["0.017"])
        target, _highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 24.75, places=1)
        self.assertEqual(_rate_string(target, prefer="99/4"), "99/4")
        self.assertAlmostEqual(dropped, 4 / 153, places=3)

    def test_all_quantized_high_rate_frames_count_toward_the_budget(self):
        """60fps 被拆成 16/17ms 两个桶时，丢帧比例要把它们一起算。"""
        durations = (["0.040404"] * 96
                     + ["0.016"] * 2 + ["0.017"] * 2)
        target, _highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 24.75, places=2)
        self.assertAlmostEqual(dropped, 0.04, places=3)

    def test_small_high_rate_burst_is_dropped(self):
        """只有 2% 的 60fps，按 24.75 压、丢掉这 2%，不把整片抬到 60。"""
        durations = ["0.040404"] * 196 + ["0.016667"] * 4
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 24.75, places=2)
        self.assertAlmostEqual(highest, 60.0, places=1)
        self.assertAlmostEqual(dropped, 0.02, places=3)

    def test_zero_budget_keeps_every_frame(self):
        durations = ["0.040404"] * 196 + ["0.016667"] * 4
        target, highest, dropped = _rate_plan_from_durations(durations, 0.0)
        self.assertAlmostEqual(target, highest, places=2)
        self.assertAlmostEqual(target, 60.0, places=1)
        self.assertEqual(dropped, 0.0)

    def test_high_rate_beyond_budget_is_not_dropped(self):
        """10% 的 60fps 超过 5% 预算，宁可整片 60fps 也不丢这一成帧。"""
        durations = ["0.040404"] * 90 + ["0.016667"] * 10
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 60.0, places=1)
        self.assertAlmostEqual(highest, 60.0, places=1)
        self.assertEqual(dropped, 0.0)

    def test_a_few_high_frames_do_not_raise_the_target(self):
        """3 帧 60fps 可能是毛刺，不能因此把整片从 24.75 抬到 60。"""
        durations = ["0.040404"] * 9997 + ["0.016667"] * 3
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 24.75, places=2)
        self.assertAlmostEqual(highest, 24.75, places=2)
        self.assertAlmostEqual(dropped, 0.0003, places=4)

    def test_one_off_timestamp_glitch_is_ignored(self):
        durations = ["0.040404"] * 100 + ["0.001"]  # 假 1000fps
        target, highest, dropped = _rate_plan_from_durations(durations, 0.05)
        self.assertAlmostEqual(target, 24.75, places=2)
        self.assertAlmostEqual(highest, 24.75, places=2)
        self.assertEqual(dropped, 0.0)

    def test_uniform_cfr_keeps_its_rate(self):
        target, highest, dropped = _rate_plan_from_durations(
            ["0.041708"] * 100, 0.05)
        self.assertAlmostEqual(target, 24000 / 1001, places=2)
        self.assertAlmostEqual(highest, 24000 / 1001, places=2)
        self.assertEqual(dropped, 0.0)

    def test_no_usable_durations_is_zero(self):
        for durations in ([], ["N/A", "0", "-1", "0.001"]):
            self.assertEqual(_rate_plan_from_durations(durations),
                             (0.0, 0.0, 0.0))

    def test_rates_are_normalised_for_ffmpeg(self):
        self.assertEqual(_rate_string(30.0003), "30")
        self.assertEqual(_rate_string(59.988), "60")
        self.assertEqual(_rate_string(24000 / 1001), "24000/1001")
        self.assertEqual(_rate_string(24.75, prefer="99/4"), "99/4")
        self.assertEqual(_rate_string(24.7503), "99/4")
        self.assertEqual(_rate_string(0), "")


class ProbeTargetRateTests(unittest.TestCase):
    """probe() 要把「按哪个帧率压」一并定下来，后续所有环节才同一口径。"""

    def setUp(self):
        self.cfg = make_cfg()
        self.path = Path("mixed.mp4")

    def stream(self, nominal, average):
        return {
            "format": {"duration": "10.0", "size": "1000000",
                       "bit_rate": "4000000", "tags": {}},
            "streams": [{
                "codec_type": "video", "codec_name": "h264",
                "width": 1920, "height": 1080,
                "r_frame_rate": nominal, "avg_frame_rate": average,
                "duration": "10.0", "pix_fmt": "yuv420p",
                "field_order": "progressive",
            }],
            "frames": [],
        }

    def run_probe(self, data, durations=None, rc=0, ratio=0.05):
        replies = [(0, json.dumps(data), "")]
        if durations is not None:
            replies.append((rc, durations, ""))
        with mock.patch.object(probe.proc, "run_managed",
                               side_effect=replies):
            return probe.probe(helpers.FakeTools(), self.path,
                               vfr_max_drop_ratio=ratio)

    def test_dominant_rate_wins_over_the_nominal_rate(self):
        info = self.run_probe(
            self.stream("99/4", "2477295/88943"),
            "0.040404\n" * 100 + "0.033333\n" * 50)
        self.assertTrue(info.vfr)
        self.assertAlmostEqual(info.fps, 24.75, places=3)
        self.assertAlmostEqual(info.avg_fps, 2477295 / 88943, places=3)
        self.assertAlmostEqual(info.vfr_max_fps, 30.0, places=2)
        self.assertEqual(info.vfr_drop_ratio, 0.0)
        self.assertEqual(info.target_fps, 30.0)
        self.assertEqual(info.target_rate, "30")
        self.assertEqual(info.encode_fps, 30.0)
        self.assertEqual(info.encode_rate, "30")

    def test_small_high_rate_burst_falls_back_to_the_nominal_rate(self):
        info = self.run_probe(
            self.stream("99/4", "2477295/88943"),
            "0.040404\n" * 196 + "0.016667\n" * 4)
        self.assertTrue(info.vfr)
        self.assertAlmostEqual(info.vfr_max_fps, 60.0, places=1)
        self.assertAlmostEqual(info.vfr_drop_ratio, 0.02, places=3)
        self.assertAlmostEqual(info.target_fps, 24.75, places=3)
        self.assertEqual(info.target_rate, "99/4")

    def test_millisecond_timebase_target_rate_is_clean(self):
        """mkv 的 33/34ms 量化不能把 30fps 目标抬成 30.303fps。"""
        info = self.run_probe(
            self.stream("99/4", "2477295/88943"),
            "0.040\n" * 95 + "0.041\n" * 54
            + "0.033\n" * 120 + "0.034\n" * 60)
        self.assertTrue(info.vfr)
        self.assertEqual(info.target_fps, 30.0)
        self.assertEqual(info.target_rate, "30")
        self.assertAlmostEqual(info.vfr_max_fps, 30.0, places=2)
        self.assertEqual(info.vfr_drop_ratio, 0.0)

    def test_zero_drop_budget_uses_the_highest_rate(self):
        info = self.run_probe(
            self.stream("99/4", "2477295/88943"),
            "0.040404\n" * 196 + "0.016667\n" * 4,
            ratio=0.0)
        self.assertAlmostEqual(info.target_fps, 60.0, places=1)
        self.assertEqual(info.target_rate, "60")
        self.assertEqual(info.vfr_drop_ratio, 0.0)

    def test_cfr_source_keeps_the_exact_rational_rate(self):
        info = self.run_probe(self.stream("24000/1001", "24000/1001"))
        self.assertFalse(info.vfr)
        self.assertEqual(info.target_rate, "24000/1001")
        self.assertAlmostEqual(info.encode_fps, 24000 / 1001, places=3)

    def test_unresolved_vfr_is_skipped_not_silently_dropped(self):
        info = self.run_probe(self.stream("99/4", "2477295/88943"),
                              "", rc=1)
        self.assertTrue(info.vfr)
        self.assertEqual(info.target_fps, 0.0)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip)
        self.assertIn("VFR", reason)

    def test_vfr_uses_average_fps_for_the_bpp_gate(self):
        """30fps 段占多数时，拿标称 24.75 算 BPP 会把码率算高。"""
        bitrate = int(0.05 * 1920 * 1080 * 26.0)  # 按 24.75 算高、按 27.85 算低
        info = make_info(fps=24.75, avg_fps=27.85, vfr=True,
                         target_fps=30.0, target_rate="30", bitrate=bitrate)
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip, reason)
        self.assertIn("低于阈值", reason)


if __name__ == "__main__":
    unittest.main()

