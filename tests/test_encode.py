import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress import encode


class AudioArgsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_normal_audio_copied(self):
        args = encode.audio_args(make_info(), self.cfg)
        self.assertEqual(args, ["-c:a", "copy"])

    def test_no_audio_empty(self):
        info = make_info(audio_codecs=[], audio_bitrates=[])
        self.assertEqual(encode.audio_args(info, self.cfg), [])

    def test_lossless_transcoded_to_opus(self):
        for codec in ("pcm_s16le", "flac", "alac", "truehd"):
            info = make_info(audio_codecs=[codec], audio_bitrates=[0],
                             audio_channels=[2])
            args = encode.audio_args(info, self.cfg)
            self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k"],
                             codec)

    def test_high_bitrate_transcoded_to_opus(self):
        info = make_info(audio_codecs=["aac"], audio_bitrates=[900_000],
                         audio_channels=[2])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k"])

    def test_bitrate_scales_with_channel_count(self):
        cases = {1: "80k", 2: "160k", 6: "480k", 8: "510k"}
        for ch, expected in cases.items():
            info = make_info(audio_codecs=["flac"], audio_bitrates=[0],
                             audio_channels=[ch])
            args = encode.audio_args(info, self.cfg)
            self.assertEqual(args[3], expected, f"{ch} channels")

    def test_only_offending_track_is_transcoded(self):
        info = make_info(audio_codecs=["truehd", "aac"],
                         audio_bitrates=[4_000_000, 192_000],
                         audio_channels=[6, 2])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "480k",
                                "-c:a:1", "copy"])

    def test_all_copyable_tracks_use_the_short_form(self):
        info = make_info(audio_codecs=["aac", "ac3"],
                         audio_bitrates=[192_000, 448_000],
                         audio_channels=[2, 6])
        self.assertEqual(encode.audio_args(info, self.cfg), ["-c:a", "copy"])

    def test_missing_channel_info_falls_back_to_stereo(self):
        info = make_info(audio_codecs=["flac"], audio_bitrates=[0],
                         audio_channels=[])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a:0", "libopus", "-b:a:0", "160k"])


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
        self.assertEqual(encode._parse_progress_line("out_time_us=4000000"), 4.0)

    def test_out_time_ms_actually_microseconds(self):
        self.assertEqual(encode._parse_progress_line("out_time_ms=1500000"), 1.5)

    def test_ignores_non_time_keys(self):
        self.assertIsNone(encode._parse_progress_line("frame=60"))
        self.assertIsNone(encode._parse_progress_line("fps=12.5"))
        self.assertIsNone(encode._parse_progress_line("progress=continue"))
        self.assertIsNone(encode._parse_progress_line("no equals sign"))

    def test_na_time_value(self):
        self.assertIsNone(encode._parse_progress_line("out_time_us=N/A"))

    def test_speed_parsing(self):
        self.assertAlmostEqual(encode._parse_speed("speed= 279x"), 279.0)
        self.assertAlmostEqual(encode._parse_speed("speed=1.25x"), 1.25)
        self.assertIsNone(encode._parse_speed("speed=N/A"))
        self.assertIsNone(encode._parse_speed("frame=60"))


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
            encode.terminate_all()
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


class PooledMeanTests(unittest.TestCase):
    def test_vmaf_metric(self):
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "min": 80}}}
        self.assertAlmostEqual(encode._pooled_mean(data), 95.5)

    def test_falls_back_to_the_only_metric(self):
        data = {"pooled_metrics": {"float_ssim": {"mean": 0.98}}}
        self.assertAlmostEqual(encode._pooled_mean(data), 0.98)

    def test_missing_pooled_metrics_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_mean({})

    def test_missing_mean_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_mean({"pooled_metrics": {"vmaf": {"min": 1}}})

    def test_non_numeric_mean_raises(self):
        with self.assertRaises(encode.EncodeError):
            encode._pooled_mean({"pooled_metrics": {"vmaf": {"mean": "n/a"}}})


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

