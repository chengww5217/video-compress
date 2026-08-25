import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress import encode, proc


class AudioArgsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def opus_cfg(self):
        cfg = make_cfg()
        cfg["audio"]["lossless"] = "opus"
        return cfg

    def test_normal_audio_copied(self):
        args = encode.audio_args(make_info(), self.cfg)
        self.assertEqual(args, ["-c:a", "copy"])

    def test_no_audio_empty(self):
        info = make_info(audio_codecs=[], audio_bitrates=[])
        self.assertEqual(encode.audio_args(info, self.cfg), [])

    def test_lossless_goes_to_flac_by_default(self):
        """压完要删原文件，无损轨不该悄悄变成有损。"""
        for codec in ("pcm_s16le", "alac", "truehd", "mlp"):
            info = make_info(audio_codecs=[codec], audio_bitrates=[3_000_000],
                             audio_channels=[6])
            args = encode.audio_args(info, self.cfg)
            self.assertEqual(args, ["-c:a:0", "flac",
                                    "-compression_level:a:0", "12"], codec)

    def test_existing_flac_is_copied_not_reencoded(self):
        info = make_info(audio_codecs=["flac"], audio_bitrates=[900_000],
                         audio_channels=[2])
        self.assertEqual(encode.audio_args(info, self.cfg), ["-c:a", "copy"])

    def test_lossless_to_opus_when_configured(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[4_000_000],
                         audio_channels=[2])
        args = encode.audio_args(info, self.opus_cfg())
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k",
                                "-filter:a:0",
                                "aformat=channel_layouts=stereo"])

    def test_mp4_output_falls_back_to_opus(self):
        """FLAC 在 mp4 里播放器支持很差，宁可用 Opus。"""
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[4_000_000],
                         audio_channels=[6])
        args = encode.audio_args(info, self.cfg, ".mp4")
        self.assertEqual(args[:4], ["-c:a:0", "libopus", "-b:a:0", "480k"])

    def test_high_bitrate_lossy_still_goes_to_opus(self):
        info = make_info(audio_codecs=["aac"], audio_bitrates=[900_000],
                         audio_channels=[2])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args[:4], ["-c:a:0", "libopus", "-b:a:0", "160k"])

    def test_bitrate_scales_with_channel_count(self):
        cases = {1: "80k", 2: "160k", 6: "480k", 8: "510k"}
        for ch, expected in cases.items():
            info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                             audio_channels=[ch])
            args = encode.audio_args(info, self.opus_cfg())
            self.assertEqual(args[3], expected, f"{ch} channels")

    def test_channel_layout_normalised_for_opus(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                         audio_channels=[6])
        args = encode.audio_args(info, self.opus_cfg())
        self.assertEqual(args[4:], ["-filter:a:0",
                                    "aformat=channel_layouts=5.1"])

    def test_exotic_channel_count_uses_arbitrary_mapping(self):
        info = make_info(audio_codecs=["pcm_s16le"], audio_bitrates=[0],
                         audio_channels=[10])
        args = encode.audio_args(info, self.opus_cfg())
        self.assertEqual(args[4:], ["-mapping_family:a:0", "255"])

    def test_only_offending_track_is_transcoded(self):
        info = make_info(audio_codecs=["truehd", "aac"],
                         audio_bitrates=[4_000_000, 192_000],
                         audio_channels=[6, 2])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a:0", "flac", "-compression_level:a:0",
                                "12", "-c:a:1", "copy"])

    def test_all_copyable_tracks_use_the_short_form(self):
        info = make_info(audio_codecs=["aac", "ac3"],
                         audio_bitrates=[192_000, 448_000],
                         audio_channels=[2, 6])
        self.assertEqual(encode.audio_args(info, self.cfg), ["-c:a", "copy"])

    def test_missing_channel_info_falls_back_to_stereo(self):
        info = make_info(audio_codecs=["truehd"], audio_bitrates=[0],
                         audio_channels=[])
        args = encode.audio_args(info, self.opus_cfg())
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k",
                                "-filter:a:0",
                                "aformat=channel_layouts=stereo"])

    def test_flac_bitrate_estimate(self):
        """PCM 转 FLAC 大约省四成；已经无损压缩过的格式基本持平。"""
        self.assertEqual(encode.flac_bitrate("pcm_s24le", 1_000_000), 600_000)
        self.assertEqual(encode.flac_bitrate("truehd", 4_000_000), 4_000_000)
        self.assertEqual(encode.flac_bitrate("truehd", 0), 0)


class VideoArgsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_av1_args(self):
        args = encode.video_args(self.cfg["encode"]["av1"], 27)
        self.assertEqual(args, [
            "-c:v", "libsvtav1",
            "-preset", "2",
            "-crf", "27",
            "-svtav1-params", "tune=0",
        ])

    def test_x265_args(self):
        args = encode.video_args(self.cfg["encode"]["x265"], 20)
        self.assertEqual(args, [
            "-c:v", "libx265",
            "-preset", "placebo",
            "-crf", "20",
            "-x265-params", "aq-mode=3",
        ])


class SubtitleArgsTests(unittest.TestCase):
    def test_no_subtitles_empty(self):
        self.assertEqual(encode.subtitle_args(make_info(), ".mkv"), [])

    def test_mov_text_converted_for_mkv(self):
        info = make_info(subtitle_codecs=["mov_text"])
        self.assertEqual(encode.subtitle_args(info, ".mkv"), ["-c:s:0", "srt"])

    def test_graphic_subs_copied_for_mkv(self):
        info = make_info(subtitle_codecs=["hdmv_pgs_subtitle", "dvd_subtitle"])
        self.assertEqual(encode.subtitle_args(info, ".mkv"),
                         ["-c:s:0", "copy", "-c:s:1", "copy"])

    def test_mixed_streams_decided_individually(self):
        info = make_info(subtitle_codecs=["mov_text", "hdmv_pgs_subtitle"])
        self.assertEqual(encode.subtitle_args(info, ".mkv"),
                         ["-c:s:0", "srt", "-c:s:1", "copy"])

    def test_text_subs_converted_for_mp4(self):
        info = make_info(subtitle_codecs=["subrip", "ass"])
        self.assertEqual(encode.subtitle_args(info, ".mp4"),
                         ["-c:s:0", "mov_text", "-c:s:1", "mov_text"])

    def test_srt_copied_as_is_for_mkv(self):
        info = make_info(subtitle_codecs=["subrip"])
        self.assertEqual(encode.subtitle_args(info, ".mkv"), ["-c:s:0", "copy"])


class BuildCmdTests(unittest.TestCase):
    def build(self, dst_name, s_args=None):
        cfg = make_cfg()
        return encode.build_cmd("ffmpeg.exe", Path("in.mp4"), Path(dst_name),
                                cfg["encode"]["av1"], 27, ["-c:a", "copy"],
                                s_args or [], "vcompress-v1")

    def test_command_structure(self):
        cmd = self.build("out.tmp.mkv", ["-c:s:0", "srt"])
        self.assertEqual(cmd[0], "ffmpeg.exe")
        self.assertIn("in.mp4", cmd)
        self.assertIn("0:V", cmd)
        self.assertIn("-map_metadata", cmd)
        self.assertIn("-map_chapters", cmd)
        self.assertIn("comment=vcompress-v1", cmd)
        self.assertIn("-crf", cmd)
        self.assertIn("27", cmd)
        self.assertIn("-c:s:0", cmd)
        self.assertEqual(cmd[-1], "out.tmp.mkv")

    def test_mkv_drops_data_streams_and_keeps_attachments(self):
        cmd = self.build("out.tmp.mkv")
        self.assertIn("-dn", cmd)
        self.assertNotIn("0:d?", cmd)
        self.assertNotIn("-c:d", cmd)
        self.assertIn("0:t?", cmd)
        self.assertIn("-c:t", cmd)

    def test_mkv_has_no_mov_only_flags(self):
        self.assertNotIn("-movflags", self.build("out.tmp.mkv"))

    def test_mp4_keeps_data_streams_and_movflags(self):
        cmd = self.build("out.tmp.mp4")
        self.assertIn("0:d?", cmd)
        self.assertIn("-c:d", cmd)
        self.assertIn("-movflags", cmd)
        self.assertNotIn("-dn", cmd)
        self.assertNotIn("0:t?", cmd)


class ParseProgressTests(unittest.TestCase):
    def test_out_time_us_is_seconds(self):
        self.assertEqual(encode._parse_progress_line("out_time_us=4000000"),
                         ("out_time_us", 4.0))

    def test_out_time_ms_actually_microseconds(self):
        self.assertEqual(encode._parse_progress_line("out_time_ms=1500000"),
                         ("out_time_ms", 1.5))

    def test_ignores_non_time_keys(self):
        self.assertIsNone(encode._parse_progress_line("frame=60"))
        self.assertIsNone(encode._parse_progress_line("fps=12.5"))
        self.assertIsNone(encode._parse_progress_line("progress=continue"))
        self.assertIsNone(encode._parse_progress_line("no equals sign"))
        self.assertIsNone(encode._parse_progress_line("out_time=00:00:01.0"))

    def test_na_time_value(self):
        self.assertIsNone(encode._parse_progress_line("out_time_us=N/A"))

    def test_speed_parsing(self):
        self.assertAlmostEqual(encode._parse_speed("speed= 279x"), 279.0)
        self.assertAlmostEqual(encode._parse_speed("speed=1.25x"), 1.25)
        self.assertIsNone(encode._parse_speed("speed=N/A"))
        self.assertIsNone(encode._parse_speed("frame=60"))


class ProgressCallbackTests(unittest.TestCase):
    """ffmpeg 每次刷新会同时发 out_time_us / out_time_ms / out_time，
    认多个键就会让同一个时刻回调多次：进度条重画、速度平滑被污染。"""

    def test_one_refresh_produces_one_callback(self):
        script = (
            "import sys\n"
            "for us in (1000000, 2000000, 3000000):\n"
            "    sys.stdout.write('frame=%d\\n' % us)\n"
            "    sys.stdout.write('out_time_us=%d\\n' % us)\n"
            "    sys.stdout.write('out_time_ms=%d\\n' % us)\n"
            "    sys.stdout.write('out_time=00:00:0%d.000000\\n' % (us // 1000000))\n"
            "    sys.stdout.write('progress=continue\\n')\n"
        )
        calls = []
        encode.run_encode([sys.executable, "-u", "-c", script], 10.0,
                          lambda pct, eta, spd: calls.append(round(pct, 3)))
        self.assertEqual(len(calls), 3, calls)
        self.assertEqual(calls, sorted(calls))

    def test_falls_back_to_whatever_key_the_build_emits(self):
        script = ("import sys\n"
                  "sys.stdout.write('out_time_ms=5000000\\n')\n")
        calls = []
        encode.run_encode([sys.executable, "-u", "-c", script], 10.0,
                          lambda pct, eta, spd: calls.append(pct))
        self.assertEqual(len(calls), 1)
        self.assertAlmostEqual(calls[0], 50.0)


class RunEncodeStderrTests(unittest.TestCase):
    """子进程往 stderr 猛写时 run_encode 不能卡死（旧实现会死锁）。"""

    def run_in_thread(self, cmd, timeout=60):
        import threading

        result = {}

        def target():
            try:
                encode.run_encode(cmd, 1.0, None)
                result["ok"] = True
            except BaseException as e:  # noqa: BLE001 - 测试里要看到任何异常
                result["err"] = e

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            proc.terminate_all()
            t.join(10)
            self.fail(f"run_encode did not finish within {timeout}s (deadlock)")
        return result

    def test_chatty_stderr_does_not_deadlock(self):
        script = (
            "import sys\n"
            "for i in range(3000):\n"
            "    sys.stderr.write('warn %d ' % i + 'x' * 100 + '\\n')\n"
            "sys.stdout.write('out_time_us=1000000\\n')\n"
        )
        res = self.run_in_thread([sys.executable, "-u", "-c", script])
        self.assertTrue(res.get("ok"), res.get("err"))

    def test_failure_keeps_stderr_tail(self):
        script = (
            "import sys\n"
            "sys.stderr.write('boom: invalid data found\\n')\n"
            "sys.exit(3)\n"
        )
        res = self.run_in_thread([sys.executable, "-u", "-c", script])
        err = res.get("err")
        self.assertIsInstance(err, encode.EncodeError)
        self.assertIn("exited 3", str(err))
        self.assertIn("boom", str(err))


class PooledScoreTests(unittest.TestCase):
    def test_prefers_harmonic_mean(self):
        """算术平均会被好帧稀释，默认取调和平均。"""
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "harmonic_mean": 93.1,
                                           "min": 80}}}
        self.assertAlmostEqual(encode._pooled_score(data), 93.1)

    def test_explicit_pool_is_honoured(self):
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "harmonic_mean": 93.1,
                                           "min": 80}}}
        self.assertAlmostEqual(encode._pooled_score(data, "min"), 80)
        self.assertAlmostEqual(encode._pooled_score(data, "mean"), 95.5)

    def test_old_libvmaf_without_harmonic_mean_falls_back(self):
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "min": 80}}}
        self.assertAlmostEqual(encode._pooled_score(data), 95.5)

    def test_named_vmaf_variant_is_found(self):
        data = {"pooled_metrics": {"integer_adm2": {"mean": 0.98},
                                   "vmaf_neg": {"mean": 91.0}}}
        self.assertAlmostEqual(encode._pooled_score(data), 91.0)

    def test_falls_back_to_the_only_metric(self):
        data = {"pooled_metrics": {"float_ssim": {"mean": 0.98}}}
        self.assertAlmostEqual(encode._pooled_score(data), 0.98)

    def test_missing_pooled_metrics_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_score({})

    def test_missing_scores_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_score({"pooled_metrics": {"vmaf": {"min": 1}}})

    def test_non_numeric_score_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_score({"pooled_metrics": {"vmaf": {"mean": "n/a"}}})


ENCODERS_SAMPLE = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC (codec h264)
 V..... libsvtav1            SVT-AV1 encoder (codec av1)
 A....D libopus              libopus Opus (codec opus)
 S..... srt                  SubRip subtitle
"""

FILTERS_SAMPLE = """Filters:
  T.. = Timeline support
 ... libvmaf           VV->V      Calculate the VMAF between two streams.
 ..C scale             V->V       Scale the input video size.
 ... amix              N->A       Audio mixing.
"""


class BuildCapabilityTests(unittest.TestCase):
    def test_encoder_names_parsed(self):
        names = encode._parse_encoder_names(ENCODERS_SAMPLE)
        self.assertEqual(names, {"libx264", "libsvtav1", "libopus", "srt"})

    def test_encoder_header_lines_ignored(self):
        self.assertNotIn("=", encode._parse_encoder_names(ENCODERS_SAMPLE))

    def test_filter_names_parsed(self):
        names = encode._parse_filter_names(FILTERS_SAMPLE)
        self.assertEqual(names, {"libvmaf", "scale", "amix"})

    def test_missing_binary_yields_empty_sets(self):
        self.assertEqual(encode.available_encoders("no-such-ffmpeg-xyz"), set())
        self.assertEqual(encode.available_filters("no-such-ffmpeg-xyz"), set())


class SegmentCmdTests(unittest.TestCase):
    def cmd(self, start, seconds=5.0):
        return encode.segment_cmd("ffmpeg", Path("in.mkv"), start, seconds,
                                  Path("out.mkv"))

    def seeks(self, cmd):
        i = cmd.index("-i")
        first = cmd.index("-ss")
        self.assertLess(first, i, "coarse -ss must come before -i")
        second = cmd.index("-ss", i)
        return float(cmd[first + 1]), float(cmd[second + 1])

    def test_two_stage_seek_adds_up_to_start(self):
        pre, post = self.seeks(self.cmd(600.0))
        self.assertAlmostEqual(pre + post, 600.0, places=3)
        self.assertAlmostEqual(post, encode.SEEK_LEADIN_S, places=3)

    def test_no_negative_seek_near_the_head(self):
        pre, post = self.seeks(self.cmd(0.5))
        self.assertEqual(pre, 0.0)
        self.assertAlmostEqual(pre + post, 0.5, places=3)

    def test_zero_start(self):
        pre, post = self.seeks(self.cmd(0.0))
        self.assertEqual((pre, post), (0.0, 0.0))

    def test_segment_is_lossless_video_only(self):
        cmd = self.cmd(10.0)
        self.assertIn("-an", cmd)
        self.assertIn("-sn", cmd)
        self.assertIn("-dn", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "0")


class FrameAlignedStartTests(unittest.TestCase):
    FPS = 24000 / 1001  # 23.976，一帧 41.708ms，mkv 的毫秒时间戳装不准

    def test_cut_sits_halfway_between_two_frames(self):
        cut = encode.frame_aligned_start(20.0, self.FPS)
        self.assertAlmostEqual((cut * self.FPS) % 1.0, 0.5, places=6)

    def test_nearby_starts_collapse_onto_the_same_cut(self):
        """帧边界两侧的请求点必须归到同一个切点，两个文件才不会错开一帧。"""
        below = encode.frame_aligned_start(20.0409, self.FPS)
        above = encode.frame_aligned_start(20.0625, self.FPS)
        self.assertAlmostEqual(below, above, places=6)

    def test_cut_stays_within_half_a_frame_of_the_request(self):
        for start in (17.3, 100.0, 4490.2):
            cut = encode.frame_aligned_start(start, self.FPS)
            self.assertLessEqual(abs(cut - start), 1.0 / self.FPS)

    def test_unknown_fps_leaves_the_start_alone(self):
        self.assertEqual(encode.frame_aligned_start(12.5, 0.0), 12.5)
        self.assertEqual(encode.frame_aligned_start(12.5, -1.0), 12.5)

    def test_never_goes_negative(self):
        self.assertEqual(encode.frame_aligned_start(0.0, self.FPS), 0.0)
        self.assertEqual(encode.frame_aligned_start(0.01, self.FPS), 0.0)
        self.assertEqual(encode.frame_aligned_start(-5.0, self.FPS), 0.0)


class VmafModelTests(unittest.TestCase):
    """默认模型按 1080p 观看条件训练，4K 片源用它是错的量具。"""

    def test_1080p_uses_the_default_model(self):
        self.assertEqual(encode.vmaf_model_for(1920, 1080), "")

    def test_1440p_still_uses_the_default_model(self):
        self.assertEqual(encode.vmaf_model_for(2560, 1440), "")

    def test_4k_switches_model(self):
        self.assertEqual(encode.vmaf_model_for(3840, 2160), encode.VMAF_MODEL_4K)

    def test_vertical_4k_switches_too(self):
        self.assertEqual(encode.vmaf_model_for(2160, 3840), encode.VMAF_MODEL_4K)

    def test_can_be_disabled(self):
        self.assertEqual(encode.vmaf_model_for(3840, 2160, False), "")

    def test_unknown_resolution_is_safe(self):
        self.assertEqual(encode.vmaf_model_for(0, 0), "")
        self.assertEqual(encode.vmaf_model_for(None, None), "")

    def test_model_goes_into_the_filter_chain(self):
        self.assertIn(f"model=version={encode.VMAF_MODEL_4K}",
                      encode.vmaf_lavfi(0, encode.VMAF_MODEL_4K))
        self.assertNotIn("model=", encode.vmaf_lavfi())


class VmafLavfiTests(unittest.TestCase):
    def test_both_inputs_are_renumbered_by_frame(self):
        lavfi = encode.vmaf_lavfi()
        self.assertEqual(lavfi.count("setpts=N/FR/TB"), 2)
        self.assertNotIn("trim", lavfi)
        self.assertIn("libvmaf", lavfi)

    def test_positive_shift_trims_the_distorted_side(self):
        lavfi = encode.vmaf_lavfi(1)
        self.assertIn("[0:v]trim=start_frame=1,setpts", lavfi)
        self.assertIn("[1:v]setpts", lavfi)

    def test_negative_shift_trims_the_reference_side(self):
        lavfi = encode.vmaf_lavfi(-2)
        self.assertIn("[1:v]trim=start_frame=2,setpts", lavfi)
        self.assertIn("[0:v]setpts", lavfi)


class BitDepthBumpTests(unittest.TestCase):
    """8-bit 源用 10-bit 编码：精度更高、暗部渐变不容易 banding，码率还略低。"""

    SVT = {"yuv420p", "yuv420p10le"}
    X265 = {"yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p",
            "yuv444p10le", "yuv420p12le"}

    def test_eight_bit_is_bumped(self):
        self.assertEqual(encode.bumped_pix_fmt("yuv420p", self.SVT, 10),
                         "yuv420p10le")

    def test_chroma_subsampling_is_preserved(self):
        """4:2:2 源在支持它的编码器上只提位深，不能顺手压成 4:2:0。"""
        self.assertEqual(encode.bumped_pix_fmt("yuv422p", self.X265, 10),
                         "yuv422p10le")
        self.assertEqual(encode.bumped_pix_fmt("yuv444p", self.X265, 10),
                         "yuv444p10le")

    def test_already_deep_enough_is_left_alone(self):
        self.assertIsNone(encode.bumped_pix_fmt("yuv420p10le", self.SVT, 10))
        self.assertIsNone(encode.bumped_pix_fmt("yuv420p12le", self.X265, 10))

    def test_disabled_by_min_depth_eight(self):
        self.assertIsNone(encode.bumped_pix_fmt("yuv420p", self.SVT, 8))

    def test_falls_back_to_a_deeper_format_when_needed(self):
        self.assertEqual(
            encode.bumped_pix_fmt("yuv420p", {"yuv420p", "yuv420p12le"}, 10),
            "yuv420p12le")

    def test_unsupported_bump_changes_nothing(self):
        self.assertIsNone(encode.bumped_pix_fmt("yuv420p", {"yuv420p"}, 10))

    def test_exotic_formats_are_left_alone(self):
        for fmt in ("nv12", "p010le", "rgb24", "gray", "yuvj420p", ""):
            self.assertIsNone(
                encode.bumped_pix_fmt(fmt, self.SVT | {"nv12", "gray"}, 10),
                fmt)

    def test_with_pix_fmt_bumps_a_supported_source(self):
        cfg = encode.with_pix_fmt(make_cfg()["encode"]["av1"], "yuv420p",
                                  self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_explicit_pix_fmt_still_wins(self):
        base = dict(make_cfg()["encode"]["av1"], pix_fmt="yuv420p")
        cfg = encode.with_pix_fmt(base, "yuv420p", self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p")


class PixFmtTests(unittest.TestCase):
    """libsvtav1 只吃 4:2:0；不归一化的话 4:2:2 源（ProRes、部分 mov）直接失败。"""

    SVT = {"yuv420p", "yuv420p10le"}
    X265 = {"yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p"}

    def test_supported_format_is_left_alone(self):
        self.assertIsNone(encode.choose_pix_fmt("yuv420p", self.SVT))
        self.assertIsNone(encode.choose_pix_fmt("yuv420p10le", self.SVT))

    def test_unknown_capabilities_change_nothing(self):
        self.assertIsNone(encode.choose_pix_fmt("yuv422p", set()))
        self.assertIsNone(encode.choose_pix_fmt("", self.SVT))

    def test_bit_depth_is_preserved_when_subsampling(self):
        self.assertEqual(encode.choose_pix_fmt("yuv422p10le", self.SVT),
                         "yuv420p10le")
        self.assertEqual(encode.choose_pix_fmt("yuv444p10le", self.SVT),
                         "yuv420p10le")

    def test_eight_bit_stays_eight_bit(self):
        self.assertEqual(encode.choose_pix_fmt("yuv422p", self.SVT), "yuv420p")

    def test_higher_depth_degrades_to_the_closest_available(self):
        self.assertEqual(encode.choose_pix_fmt("yuv420p12le", self.SVT),
                         "yuv420p10le")

    def test_x265_keeps_422(self):
        self.assertIsNone(encode.choose_pix_fmt("yuv422p", self.X265))

    def test_depth_parsing(self):
        self.assertEqual(encode.pix_fmt_depth("yuv420p"), 8)
        self.assertEqual(encode.pix_fmt_depth("yuv420p10le"), 10)
        self.assertEqual(encode.pix_fmt_depth("yuv444p12be"), 12)
        self.assertEqual(encode.pix_fmt_depth(""), 8)

    def test_with_pix_fmt_adds_the_flag_and_warns(self):
        warnings = []
        log = _FakeLog(warnings)
        base = dict(make_cfg()["encode"]["av1"], min_bit_depth=8)
        cfg = encode.with_pix_fmt(base, "yuv422p", self.SVT, log)
        self.assertEqual(cfg["pix_fmt"], "yuv420p")
        self.assertIn("yuv422p", warnings[0])
        self.assertIn("-pix_fmt", encode.video_args(cfg, 30))
        self.assertEqual(encode.video_args(cfg, 30)[7], "yuv420p")

    def test_subsampled_source_is_also_bumped_to_10_bit(self):
        """4:2:2 8-bit 源：色度降到 4:2:0，位深顺手提到 10-bit。"""
        cfg = encode.with_pix_fmt(make_cfg()["encode"]["av1"], "yuv422p",
                                  self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_with_pix_fmt_respects_an_explicit_setting(self):
        base = dict(make_cfg()["encode"]["av1"], pix_fmt="yuv420p10le")
        cfg = encode.with_pix_fmt(base, "yuv422p", self.SVT, None)
        self.assertEqual(cfg["pix_fmt"], "yuv420p10le")

    def test_with_pix_fmt_does_not_mutate_the_input(self):
        base = make_cfg()["encode"]["av1"]
        encode.with_pix_fmt(base, "yuv422p", self.SVT, None)
        self.assertNotIn("pix_fmt", base)

    def test_hopeless_format_warns_instead_of_guessing(self):
        warnings = []
        cfg = encode.with_pix_fmt(make_cfg()["encode"]["av1"], "rgb24",
                                  {"gray"}, _FakeLog(warnings))
        self.assertNotIn("pix_fmt", cfg)
        self.assertIn("may fail", warnings[0])


class _FakeLog:
    def __init__(self, sink):
        self.sink = sink

    def warning(self, msg):
        self.sink.append(str(msg))

    def info(self, msg):
        pass

    def debug(self, msg):
        pass


class MergeCommentTests(unittest.TestCase):
    """-map_metadata 0 会带来原始 comment，标记要追加而不是覆盖。"""

    def test_empty_original_gets_just_the_marker(self):
        self.assertEqual(encode.merge_comment("", "vcompress-v1"),
                         "vcompress-v1")
        self.assertEqual(encode.merge_comment(None, "vcompress-v1"),
                         "vcompress-v1")

    def test_existing_comment_is_preserved(self):
        got = encode.merge_comment("ripped by someone", "vcompress-v1")
        self.assertIn("ripped by someone", got)
        self.assertIn("vcompress-v1", got)

    def test_marker_is_not_appended_twice(self):
        once = encode.merge_comment("note | vcompress-v1", "vcompress-v1")
        self.assertEqual(once.count("vcompress-v1"), 1)

    def test_build_cmd_carries_the_original_comment(self):
        cfg = make_cfg()
        cmd = encode.build_cmd("ffmpeg", Path("in.mp4"), Path("out.tmp.mkv"),
                               cfg["encode"]["av1"], 27, [], [],
                               "vcompress-v1", "director's cut")
        value = cmd[cmd.index("-metadata") + 1]
        self.assertIn("director's cut", value)
        self.assertIn("vcompress-v1", value)


class CapsTests(unittest.TestCase):
    def test_x264_is_preferred_for_sampling(self):
        caps = encode.Caps(encoders=frozenset({"libx264", "libsvtav1"}))
        self.assertEqual(caps.lossless_codec, "libx264")

    def test_ffv1_is_the_fallback(self):
        caps = encode.Caps(encoders=frozenset({"libsvtav1"}))
        self.assertEqual(caps.lossless_codec, "ffv1")

    def test_unknown_build_assumes_x264(self):
        self.assertFalse(encode.Caps().known)
        self.assertEqual(encode.Caps().lossless_codec, "libx264")

    def test_lossless_args(self):
        self.assertEqual(encode.lossless_args("libx264"),
                         ["-c:v", "libx264", "-crf", "0", "-preset", "ultrafast"])
        self.assertEqual(encode.lossless_args("ffv1"),
                         ["-c:v", "ffv1", "-level", "1"])


class SegmentStreamSelectionTests(unittest.TestCase):
    def test_attached_pictures_are_excluded(self):
        """0:v:0 会把封面图当视频流；真编码用的是 0:V，两边必须取同一条流。"""
        cmd = encode.segment_cmd("ffmpeg", Path("in.mp4"), 10.0, 5.0,
                                 Path("o.mkv"))
        self.assertIn("0:V:0", cmd)
        self.assertNotIn("0:v:0", cmd)

    def test_codec_is_configurable(self):
        cmd = encode.segment_cmd("ffmpeg", Path("in.mp4"), 10.0, 5.0,
                                 Path("o.mkv"), "ffv1")
        self.assertIn("ffv1", cmd)
        self.assertNotIn("-crf", cmd)


class SampleStartsTests(unittest.TestCase):
    """搜索样本必须铺开：只看中点会被一段明亮动作戏或全黑过场带偏。"""

    def test_segments_are_spread_over_the_file(self):
        starts = encode.sample_starts(1200.0, 10.0, 4)
        self.assertEqual([round(s) for s in starts], [145, 445, 745, 1045])

    def test_single_segment_lands_in_the_middle(self):
        self.assertEqual(encode.sample_starts(600.0, 30.0, 1), [285.0])

    def test_never_starts_past_the_end(self):
        for start in encode.sample_starts(20.0, 10.0, 4):
            self.assertLessEqual(start, 10.0)
            self.assertGreaterEqual(start, 0.0)

    def test_short_file_collapses_to_one_segment(self):
        """片子比样本还短时几段会挤在一起，别把同一段编四遍。"""
        self.assertEqual(encode.sample_starts(8.0, 10.0, 4), [0.0])

    def test_unknown_duration_is_safe(self):
        self.assertEqual(encode.sample_starts(0.0, 10.0, 4), [0.0])

    def test_count_is_clamped_to_at_least_one(self):
        self.assertEqual(len(encode.sample_starts(600.0, 10.0, 0)), 1)


class ProbeCrfTests(unittest.TestCase):
    """一个 CRF 的分数取最差段，而不是平均。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.refs = [self.root / f"ref{i}.mkv" for i in range(3)]
        self.encoded = []
        self.orig_encode = encode.encode_sample
        self.orig_score = encode.vmaf_score
        encode.encode_sample = self.fake_encode
        encode.vmaf_score = self.fake_score
        self.log = logging.getLogger("probe-crf-test")
        self.log.addHandler(logging.NullHandler())

    def fake_encode(self, ff, ref, crf, cfg, out):
        self.encoded.append(Path(ref).name)
        Path(out).write_bytes(b"x" * 1000)

    def tearDown(self):
        encode.encode_sample = self.orig_encode
        encode.vmaf_score = self.orig_score
        self.tmp.cleanup()

    def fake_score(self, ff, dist, ref, shift=0, **kw):
        return self.scores[len(self.encoded) - 1]

    def probe(self, crf=30, target=95.0):
        return encode._probe_crf(helpers.FakeTools(), self.refs, crf, {},
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


class PredictedSizeTests(unittest.TestCase):
    """编码前的体积预估：宁可估不出来，也不要瞎猜。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_video_plus_audio(self):
        info = make_info(duration=100.0, audio_bitrates=[200_000],
                         audio_codecs=["aac"])
        # (2_000_000 + 200_000) bps * 100s / 8
        self.assertEqual(encode.predicted_size(info, self.cfg, 2_000_000),
                         27_500_000)

    def test_lossless_track_counted_at_its_flac_bitrate(self):
        info = make_info(duration=100.0, audio_codecs=["truehd"],
                         audio_bitrates=[4_000_000], audio_channels=[6])
        # TrueHD -> FLAC 体积基本持平，按 4 Mbps 算
        self.assertEqual(encode.predicted_size(info, self.cfg, 1_000_000),
                         int((1_000_000 + 4_000_000) * 100 / 8))

    def test_lossless_track_counted_at_its_opus_bitrate(self):
        cfg = make_cfg()
        cfg["audio"]["lossless"] = "opus"
        info = make_info(duration=100.0, audio_codecs=["truehd"],
                         audio_bitrates=[4_000_000], audio_channels=[6])
        # 6 声道 Opus = 480k，不是原来的 4 Mbps
        self.assertEqual(encode.predicted_size(info, cfg, 1_000_000),
                         int((1_000_000 + 480_000) * 100 / 8))

    def test_unknown_audio_bitrate_gives_up(self):
        info = make_info(duration=100.0, audio_codecs=["aac"],
                         audio_bitrates=[0])
        self.assertIsNone(encode.predicted_size(info, self.cfg, 2_000_000))

    def test_unknown_duration_or_bitrate_gives_up(self):
        self.assertIsNone(encode.predicted_size(make_info(duration=0.0),
                                                self.cfg, 2_000_000))
        self.assertIsNone(encode.predicted_size(make_info(), self.cfg, 0))

    def test_no_audio_is_fine(self):
        info = make_info(duration=100.0, audio_codecs=[], audio_bitrates=[],
                         audio_channels=[])
        self.assertEqual(encode.predicted_size(info, self.cfg, 800_000),
                         10_000_000)


class AudioPlanTests(unittest.TestCase):
    """预估和实际编码参数必须出自同一套决定。"""

    def setUp(self):
        self.cfg = make_cfg()

    def test_plan_matches_args_for_mixed_tracks(self):
        info = make_info(audio_codecs=["aac", "flac", "dts"],
                         audio_bitrates=[192_000, 3_000_000, 768_000],
                         audio_channels=[2, 2, 6])
        plan = encode.audio_plan(info, self.cfg)
        self.assertEqual([kind for kind, _br in plan],
                         ["copy", "copy", "opus"])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args[args.index("-c:a:0") + 1], "copy")
        self.assertEqual(args[args.index("-c:a:1") + 1], "copy")
        self.assertEqual(args[args.index("-b:a:2") + 1], "480k")


class SampleBitrateTests(unittest.TestCase):
    def test_bytes_over_total_sample_seconds(self):
        self.assertEqual(encode._sample_bitrate(1_000_000, 10.0, 4), 200_000)

    def test_degenerate_inputs(self):
        self.assertEqual(encode._sample_bitrate(0, 10.0, 4), 0)
        self.assertEqual(encode._sample_bitrate(1000, 0.0, 4), 0)
        self.assertEqual(encode._sample_bitrate(1000, 10.0, 0), 0)


class AdaptiveFallbackTests(unittest.TestCase):
    def test_missing_binary_returns_none(self):
        import logging

        log = logging.getLogger("test")
        log.addHandler(logging.NullHandler())
        class FakeTools:
            ffmpeg = "definitely-not-a-real-binary-xyz"
            ffprobe = "definitely-not-a-real-binary-xyz"

        picked = encode.pick_crf_adaptive(
            FakeTools(), Path("whatever.mp4"), 60.0,
            make_cfg()["encode"]["av1"],
            make_cfg()["adaptive"], log,
        )
        self.assertIsNone(picked)


if __name__ == "__main__":
    unittest.main()

