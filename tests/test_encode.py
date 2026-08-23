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
