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
                             _max_rate_from_durations, _parse_fps,
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
    """混合帧率片源：按最高帧率做 CFR，否则高帧率段会被悄悄抽帧。"""

    def test_detects_mixed_frame_rate(self):
        self.assertTrue(_detect_vfr(99 / 4, 2477295 / 88943))
        self.assertTrue(_detect_vfr(24.0, 30.0))

    def test_cfr_rounding_noise_is_not_vfr(self):
        # 29.97 被写成 30、mkv 的毫秒时间基，都不是混合帧率
        self.assertFalse(_detect_vfr(30.0, 29.97))
        self.assertFalse(_detect_vfr(24000 / 1001, 24000 / 1001))
        self.assertFalse(_detect_vfr(0.0, 30.0))
        self.assertFalse(_detect_vfr(30.0, 0.0))

    def test_mixed_durations_pick_the_highest_rate(self):
        durations = ["0.040404"] * 100 + ["0.033333"] * 50
        self.assertAlmostEqual(_max_rate_from_durations(durations), 30.0,
                               places=2)

    def test_one_off_timestamp_glitch_is_ignored(self):
        durations = ["0.040404"] * 100 + ["0.001"]  # 假 1000fps
        self.assertAlmostEqual(_max_rate_from_durations(durations), 24.75,
                               places=2)

    def test_uniform_cfr_keeps_its_rate(self):
        self.assertAlmostEqual(
            _max_rate_from_durations(["0.041708"] * 100), 24000 / 1001,
            places=2)

    def test_no_usable_durations_is_zero(self):
        self.assertEqual(_max_rate_from_durations([]), 0.0)
        self.assertEqual(_max_rate_from_durations(["N/A", "0", "-1"]), 0.0)

    def test_rates_are_normalised_for_ffmpeg(self):
        self.assertEqual(_rate_string(30.0003), "30")
        self.assertEqual(_rate_string(24000 / 1001), "24000/1001")
        self.assertEqual(_rate_string(24.75, prefer="99/4"), "99/4")
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

    def run_probe(self, data, durations=None, rc=0):
        replies = [(0, json.dumps(data), "")]
        if durations is not None:
            replies.append((rc, durations, ""))
        with mock.patch.object(probe.proc, "run_managed",
                               side_effect=replies):
            return probe.probe(helpers.FakeTools(), self.path)

    def test_mixed_frame_rate_uses_the_highest_rate(self):
        info = self.run_probe(
            self.stream("99/4", "2477295/88943"),
            "0.040404\n" * 100 + "0.033333\n" * 50)
        self.assertTrue(info.vfr)
        self.assertAlmostEqual(info.fps, 24.75, places=3)
        self.assertAlmostEqual(info.avg_fps, 2477295 / 88943, places=3)
        self.assertEqual(info.target_fps, 30.0)
        self.assertEqual(info.target_rate, "30")
        self.assertEqual(info.encode_fps, 30.0)
        self.assertEqual(info.encode_rate, "30")

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

