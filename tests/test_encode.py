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
        for codec in ("pcm_s16le", "flac", "alac"):
            info = make_info(audio_codecs=[codec], audio_bitrates=[0])
            args = encode.audio_args(info, self.cfg)
            self.assertEqual(args, ["-c:a", "libopus", "-b:a", "160k"], codec)

    def test_high_bitrate_transcoded_to_opus(self):
        info = make_info(audio_codecs=["aac"], audio_bitrates=[900_000])
        args = encode.audio_args(info, self.cfg)
        self.assertEqual(args, ["-c:a", "libopus", "-b:a", "160k"])


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


class BuildCmdTests(unittest.TestCase):
    def test_command_structure(self):
        cfg = make_cfg()
        src = Path("in.mp4")
        dst = Path("out.tmp.mkv")
        cmd = encode.build_cmd("ffmpeg.exe", src, dst,
                               cfg["encode"]["av1"], 27,
                               ["-c:a", "copy"], "vcompress-v1")
        self.assertEqual(cmd[0], "ffmpeg.exe")
        self.assertIn(str(src), cmd)
        self.assertIn("0:V", cmd)
        self.assertIn("-map_metadata", cmd)
        self.assertIn("-map_chapters", cmd)
        self.assertIn("comment=vcompress-v1", cmd)
        self.assertIn("-crf", cmd)
        self.assertIn("27", cmd)
        self.assertIn("-c:s", cmd)
        self.assertEqual(cmd[-1], str(dst))


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

