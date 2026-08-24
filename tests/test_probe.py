import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

import helpers
from helpers import make_cfg, make_info
from vcompress.probe import (_parse_fps, _stream_bitrate, _to_int, should_skip)


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


if __name__ == "__main__":
    unittest.main()

