import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress.probe import (_color_tag, _hdr_markers, _parse_fps,
                             _stream_bitrate, should_skip)
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
        self.assertIn("processed", reason)

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

    def test_fat_audio_track_does_not_hide_a_thin_video_track(self):
        # 容器总码率 3.6M 看着够高，但其中 1.5M 是音轨，视频其实已压到位
        info = make_info(bitrate=0, container_bitrate=3_600_000,
                         audio_codecs=["truehd"], audio_bitrates=[1_500_000])
        skip, reason = should_skip(info, self.cfg)
        self.assertTrue(skip, "bpp gate must judge the video track alone")
        self.assertIn("bits/pixel", reason)


class InterlacedTests(unittest.TestCase):
    """交错源必须跳过：压过去会丢交错标记，而这是 VMAF 测不出来的视觉有损。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_interlaced_source_skips(self):
        for order in ("tt", "bb", "tb", "bt"):
            skip, reason = should_skip(make_info(field_order=order), self.cfg)
            self.assertTrue(skip, order)
            self.assertIn("interlaced", reason)
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
        self.assertIn("interlaced", reason)

    def test_interlaced_property(self):
        self.assertTrue(make_info(field_order="bt").interlaced)
        self.assertFalse(make_info(field_order="progressive").interlaced)
        # 大小写在探测时就规范化了，这里只认小写
        self.assertFalse(make_info(field_order="BT").interlaced)


if __name__ == "__main__":
    unittest.main()

