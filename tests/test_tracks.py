"""音轨/字幕处置与体积预估。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

from helpers import make_cfg, make_info
from vcompress import tracks


class AudioArgsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def opus_cfg(self):
        cfg = make_cfg()
        cfg["audio"]["lossless"] = "opus"
        return cfg

    def test_normal_audio_copied(self):
        args = tracks.audio_args(make_info(), self.cfg)
        self.assertEqual(args, ["-c:a", "copy"])

    def test_no_audio_empty(self):
        info = make_info(audio_codecs=[], audio_bitrates=[])
        self.assertEqual(tracks.audio_args(info, self.cfg), [])

    def test_lossless_goes_to_flac_by_default(self):
        """压完要删原文件，无损轨不该悄悄变成有损。"""
        for codec in ("pcm_s16le", "alac", "truehd", "mlp"):
            info = make_info(audio_codecs=[codec], audio_bitrates=[3_000_000],
                             audio_channels=[6])
            args = tracks.audio_args(info, self.cfg)
            self.assertEqual(args, ["-c:a:0", "flac",
                                    "-compression_level:a:0", "12"], codec)

    def test_existing_flac_is_copied_not_reencoded(self):
        info = make_info(audio_codecs=["flac"], audio_bitrates=[900_000],
                         audio_channels=[2])
        self.assertEqual(tracks.audio_args(info, self.cfg), ["-c:a", "copy"])

    def test_lossless_to_opus_when_configured(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[4_000_000],
                         audio_channels=[2])
        args = tracks.audio_args(info, self.opus_cfg())
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k",
                                "-filter:a:0",
                                "aformat=channel_layouts=stereo"])

    def test_mp4_output_falls_back_to_opus(self):
        """FLAC 在 mp4 里播放器支持很差，宁可用 Opus。"""
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[4_000_000],
                         audio_channels=[6])
        args = tracks.audio_args(info, self.cfg, ".mp4")
        self.assertEqual(args[:4], ["-c:a:0", "libopus", "-b:a:0", "480k"])

    def test_high_bitrate_lossy_still_goes_to_opus(self):
        info = make_info(audio_codecs=["aac"], audio_bitrates=[900_000],
                         audio_channels=[2])
        args = tracks.audio_args(info, self.cfg)
        self.assertEqual(args[:4], ["-c:a:0", "libopus", "-b:a:0", "160k"])

    def test_bitrate_scales_with_channel_count(self):
        cases = {1: "80k", 2: "160k", 6: "480k", 8: "510k"}
        for ch, expected in cases.items():
            info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                             audio_channels=[ch])
            args = tracks.audio_args(info, self.opus_cfg())
            self.assertEqual(args[3], expected, f"{ch} channels")

    def test_channel_layout_normalised_for_opus(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                         audio_channels=[6])
        args = tracks.audio_args(info, self.opus_cfg())
        self.assertEqual(args[4:], ["-filter:a:0",
                                    "aformat=channel_layouts=5.1"])

    def test_exotic_channel_count_uses_arbitrary_mapping(self):
        info = make_info(audio_codecs=["pcm_s16le"], audio_bitrates=[0],
                         audio_channels=[10])
        args = tracks.audio_args(info, self.opus_cfg())
        self.assertEqual(args[4:], ["-mapping_family:a:0", "255"])

    def test_only_offending_track_is_transcoded(self):
        info = make_info(audio_codecs=["truehd", "aac"],
                         audio_bitrates=[4_000_000, 192_000],
                         audio_channels=[6, 2])
        args = tracks.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a:0", "flac", "-compression_level:a:0",
                                "12", "-c:a:1", "copy"])

    def test_all_copyable_tracks_use_the_short_form(self):
        info = make_info(audio_codecs=["aac", "ac3"],
                         audio_bitrates=[192_000, 448_000],
                         audio_channels=[2, 6])
        self.assertEqual(tracks.audio_args(info, self.cfg), ["-c:a", "copy"])

    def test_missing_channel_info_falls_back_to_stereo(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                         audio_channels=[])
        args = tracks.audio_args(info, self.opus_cfg())
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k",
                                "-filter:a:0",
                                "aformat=channel_layouts=stereo"])

    def test_flac_bitrate_estimate(self):
        """PCM 转 FLAC 大约省四成；已经无损压缩过的格式基本持平。"""
        self.assertEqual(tracks.flac_bitrate("pcm_s24le", 1_000_000), 600_000)
        self.assertEqual(tracks.flac_bitrate("truehd", 4_000_000), 4_000_000)
        self.assertEqual(tracks.flac_bitrate("truehd", 0), 0)

class SubtitleArgsTests(unittest.TestCase):
    def test_no_subtitles_empty(self):
        self.assertEqual(tracks.subtitle_args(make_info(), ".mkv"), [])

    def test_mov_text_converted_for_mkv(self):
        info = make_info(subtitle_codecs=["mov_text"])
        self.assertEqual(tracks.subtitle_args(info, ".mkv"), ["-c:s:0", "srt"])

    def test_graphic_subs_copied_for_mkv(self):
        info = make_info(subtitle_codecs=["hdmv_pgs_subtitle", "dvd_subtitle"])
        self.assertEqual(tracks.subtitle_args(info, ".mkv"),
                         ["-c:s:0", "copy", "-c:s:1", "copy"])

    def test_mixed_streams_decided_individually(self):
        info = make_info(subtitle_codecs=["mov_text", "hdmv_pgs_subtitle"])
        self.assertEqual(tracks.subtitle_args(info, ".mkv"),
                         ["-c:s:0", "srt", "-c:s:1", "copy"])

    def test_text_subs_converted_for_mp4(self):
        info = make_info(subtitle_codecs=["subrip", "ass"])
        self.assertEqual(tracks.subtitle_args(info, ".mp4"),
                         ["-c:s:0", "mov_text", "-c:s:1", "mov_text"])

    def test_srt_copied_as_is_for_mkv(self):
        info = make_info(subtitle_codecs=["subrip"])
        self.assertEqual(tracks.subtitle_args(info, ".mkv"), ["-c:s:0", "copy"])

class PredictedSizeTests(unittest.TestCase):
    """编码前的体积预估：宁可估不出来，也不要瞎猜。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_video_plus_audio(self):
        info = make_info(duration=100.0, audio_bitrates=[200_000],
                         audio_codecs=["aac"])
        # (2_000_000 + 200_000) bps * 100s / 8
        self.assertEqual(tracks.predicted_size(info, self.cfg, 2_000_000),
                         27_500_000)

    def test_lossless_track_counted_at_its_flac_bitrate(self):
        info = make_info(duration=100.0, audio_codecs=["truehd"],
                         audio_bitrates=[4_000_000], audio_channels=[6])
        # TrueHD -> FLAC 体积基本持平，按 4 Mbps 算
        self.assertEqual(tracks.predicted_size(info, self.cfg, 1_000_000),
                         int((1_000_000 + 4_000_000) * 100 / 8))

    def test_lossless_track_counted_at_its_opus_bitrate(self):
        cfg = make_cfg()
        cfg["audio"]["lossless"] = "opus"
        info = make_info(duration=100.0, audio_codecs=["truehd"],
                         audio_bitrates=[4_000_000], audio_channels=[6])
        # 6 声道 Opus = 480k，不是原来的 4 Mbps
        self.assertEqual(tracks.predicted_size(info, cfg, 1_000_000),
                         int((1_000_000 + 480_000) * 100 / 8))

    def test_unknown_audio_bitrate_gives_up(self):
        info = make_info(duration=100.0, audio_codecs=["aac"],
                         audio_bitrates=[0])
        self.assertIsNone(tracks.predicted_size(info, self.cfg, 2_000_000))

    def test_unknown_duration_or_bitrate_gives_up(self):
        self.assertIsNone(tracks.predicted_size(make_info(duration=0.0),
                                                self.cfg, 2_000_000))
        self.assertIsNone(tracks.predicted_size(make_info(), self.cfg, 0))

    def test_no_audio_is_fine(self):
        info = make_info(duration=100.0, audio_codecs=[], audio_bitrates=[],
                         audio_channels=[])
        self.assertEqual(tracks.predicted_size(info, self.cfg, 800_000),
                         10_000_000)

class AudioPlanTests(unittest.TestCase):
    """预估和实际编码参数必须出自同一套决定。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_plan_matches_args_for_mixed_tracks(self):
        info = make_info(audio_codecs=["aac", "flac", "dts"],
                         audio_bitrates=[192_000, 3_000_000, 768_000],
                         audio_channels=[2, 2, 6])
        plan = tracks.audio_plan(info, self.cfg)
        self.assertEqual([kind for kind, _br in plan],
                         ["copy", "copy", "opus"])
        args = tracks.audio_args(info, self.cfg)
        self.assertEqual(args[args.index("-c:a:0") + 1], "copy")
        self.assertEqual(args[args.index("-c:a:1") + 1], "copy")
        self.assertEqual(args[args.index("-b:a:2") + 1], "480k")


if __name__ == "__main__":
    unittest.main()
