"""编码参数生成与编码执行。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

from helpers import make_cfg
from vcompress import encode, proc


class FractionalCrfTests(unittest.TestCase):
    """1 级 CRF ≈ 8% 体积，只搜整数等于平均白扔半级 ≈ 4%。"""

    def av1(self, **kw):
        return dict(make_cfg()["encode"]["av1"], **kw)

    def test_quantize_to_quarter_steps(self):
        self.assertEqual(encode.quantize_crf(35.1), 35)
        self.assertEqual(encode.quantize_crf(35.2), 35.25)
        self.assertEqual(encode.quantize_crf(35.9), 36)
        self.assertEqual(encode.quantize_crf(35), 35)

    def test_integer_crf_uses_the_plain_flag(self):
        args = encode.video_args(self.av1(extra="-svtav1-params tune=0"), 35)
        self.assertEqual(args[args.index("-crf") + 1], "35")
        self.assertEqual(args[args.index("-svtav1-params") + 1], "tune=0")

    def test_svtav1_fraction_goes_into_the_params(self):
        """-crf 对 libsvtav1 是整数选项，小数只能走 svtav1-params。"""
        args = encode.video_args(self.av1(extra="-svtav1-params tune=0"), 35.75)
        self.assertEqual(args[args.index("-svtav1-params") + 1],
                         "tune=0:crf=35.75")
        # 整数那个留着给忽略 params 的老构建兜底（实测 params 会覆盖它）
        self.assertEqual(args[args.index("-crf") + 1], "35")

    def test_existing_tuning_params_are_kept(self):
        extra = ("-svtav1-params tune=0:enable-variance-boost=1:"
                 "qp-scale-compress-strength=1")
        args = encode.video_args(self.av1(extra=extra), 41.25)
        params = args[args.index("-svtav1-params") + 1]
        self.assertTrue(params.startswith("tune=0:enable-variance-boost=1:"
                                          "qp-scale-compress-strength=1"))
        self.assertTrue(params.endswith(":crf=41.25"))

    def test_params_switch_is_added_when_missing(self):
        args = encode.video_args(self.av1(extra=""), 30.5)
        self.assertEqual(args[args.index("-svtav1-params") + 1], "crf=30.5")

    def test_an_existing_crf_param_is_replaced_not_duplicated(self):
        args = encode.video_args(
            self.av1(extra="-svtav1-params crf=20:tune=0"), 30.5)
        params = args[args.index("-svtav1-params") + 1]
        self.assertEqual(params.count("crf="), 1)
        self.assertIn("tune=0", params)
        self.assertIn("crf=30.5", params)

    def test_x265_takes_a_float_crf_directly(self):
        cfg = dict(make_cfg()["encode"]["x265"])
        args = encode.video_args(cfg, 27.75)
        self.assertEqual(args[args.index("-crf") + 1], "27.75")
        self.assertNotIn("-svtav1-params", args)

    def test_unknown_encoder_falls_back_to_an_integer(self):
        cfg = {"codec": "libvpx-vp9", "preset": "0", "crf": 30, "extra": ""}
        args = encode.video_args(cfg, 30.75)
        self.assertEqual(args[args.index("-crf") + 1], "31")

    def test_config_is_not_mutated(self):
        cfg = self.av1(extra="-svtav1-params tune=0")
        encode.video_args(cfg, 35.75)
        self.assertEqual(cfg["extra"], "-svtav1-params tune=0")

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


if __name__ == "__main__":
    unittest.main()
