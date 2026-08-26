import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import make_cfg, make_info
from vcompress import caps, cli, power, proc
from vcompress.logfmt import em
from vcompress.results import ResultStore


class ParserTests(unittest.TestCase):
    def test_defaults(self):
        args = cli.build_parser().parse_args(["a.mp4"])
        self.assertEqual(args.paths, ["a.mp4"])
        self.assertTrue(args.recursive)
        self.assertTrue(args.adaptive)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.encoder)
        self.assertIsNone(args.config)

    def test_flags(self):
        args = cli.build_parser().parse_args(
            ["a.mp4", "b.mkv", "--dry-run", "--no-recursive", "--no-adaptive",
             "--keep", "--recycle", "--force", "--workers", "3",
             "--encoder", "x265"])
        self.assertEqual(args.paths, ["a.mp4", "b.mkv"])
        self.assertFalse(args.recursive)
        self.assertFalse(args.adaptive)
        self.assertTrue(args.dry_run)
        self.assertTrue(args.keep)
        self.assertTrue(args.recycle)
        self.assertTrue(args.force)
        self.assertEqual(args.workers, 3)
        self.assertEqual(args.encoder, "x265")

    def test_encoder_is_not_a_hardcoded_choice(self):
        """编码器候选来自配置，argparse 不该先把没见过的名字拒掉。"""
        args = cli.build_parser().parse_args(["a.mp4", "--encoder", "vp9"])
        self.assertEqual(args.encoder, "vp9")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.logs = []
        self.handler = _CaptureHandler(self.logs)
        logging.getLogger("vcompress").addHandler(self.handler)

    def tearDown(self):
        logging.getLogger("vcompress").removeHandler(self.handler)

    def run_preflight(self, encoders, filters, adaptive=True):
        built = caps.Caps(encoders=frozenset(encoders),
                          filters=frozenset(filters))
        return cli.preflight(built, self.cfg, "av1", adaptive), built

    def messages(self):
        return "\n".join(self.logs)

    def test_missing_video_codec_aborts(self):
        ok, _ = self.run_preflight({"libx264", "libopus"}, {"libvmaf"})
        self.assertFalse(ok)
        self.assertIn("libsvtav1", self.messages())

    def test_full_build_passes_without_warnings(self):
        ok, _ = self.run_preflight({"libsvtav1", "libx264", "libopus"},
                                   {"libvmaf", "scale"})
        self.assertTrue(ok)
        self.assertEqual(self.logs, [])

    def test_missing_opus_only_warns(self):
        ok, _ = self.run_preflight({"libsvtav1", "libx264"}, {"libvmaf"})
        self.assertTrue(ok)
        self.assertIn("libopus", self.messages())

    def test_missing_vmaf_only_warns(self):
        ok, _ = self.run_preflight({"libsvtav1", "libx264", "libopus"},
                                   {"scale"})
        self.assertTrue(ok)
        self.assertIn("libvmaf", self.messages())

    def test_missing_x264_falls_back_to_ffv1(self):
        ok, caps = self.run_preflight({"libsvtav1", "libopus"}, {"libvmaf"})
        self.assertTrue(ok)
        self.assertEqual(caps.lossless_codec, "ffv1")
        self.assertIn("ffv1", self.messages())

    def test_unknown_build_skips_the_check(self):
        ok, _ = self.run_preflight(set(), set())
        self.assertTrue(ok)
        self.assertIn("跳过构建自检", self.messages())


class _CaptureHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__(level=logging.WARNING)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


class LoggingSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = logging.getLogger().handlers[:]

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in self.saved:
            root.addHandler(h)
        self.tmp.cleanup()

    def silence_console(self):
        """只留文件 handler，别把几百行测试日志喷到测试输出里。"""
        root = logging.getLogger()
        for h in root.handlers[:]:
            if isinstance(h, logging.StreamHandler) and \
                    not isinstance(h, logging.FileHandler):
                root.removeHandler(h)

    def test_creates_the_log_directory(self):
        target = self.root / "deep" / "logs" / "vc.log"
        cli.setup_logging(target)
        self.silence_console()
        logging.getLogger("vcompress").info("hello")
        self.assertTrue(target.exists())
        self.assertIn("hello", target.read_text(encoding="utf-8"))

    def test_repeated_setup_does_not_duplicate_handlers(self):
        target = self.root / "vc.log"
        cli.setup_logging(target)
        first = len(logging.getLogger().handlers)
        cli.setup_logging(target)
        self.assertEqual(len(logging.getLogger().handlers), first,
                         "handlers accumulated across setup_logging calls")

    def test_each_start_writes_a_session_banner(self):
        """日志是追加写的，每次启动要有一条好找的分隔线。"""
        target = self.root / "vc.log"
        cli.setup_logging(target, argv=["vcompress", "/videos"])
        self.silence_console()
        logging.getLogger("vcompress").info("第一次")
        cli.setup_logging(target, argv=["vcompress", "/videos", "--dry-run"])
        self.silence_console()
        logging.getLogger("vcompress").info("第二次")
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count("启动程序 ===="), 2, text)
        self.assertIn("命令行: vcompress /videos --dry-run", text)
        self.assertIn("\n\n\n====", text)

    def test_the_file_log_has_no_ansi_escapes(self):
        target = self.root / "vc.log"
        cli.setup_logging(target)
        self.silence_console()
        logging.getLogger("vcompress").warning(f"清掉 {em('a.tmp.mkv')}")
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("\x1b", text)
        self.assertIn("清掉 a.tmp.mkv", text)

    def test_log_file_is_rotated_instead_of_growing_forever(self):
        target = self.root / "vc.log"
        cli.setup_logging(target, max_bytes=2048, backups=2)
        self.silence_console()
        log = logging.getLogger("vcompress")
        for i in range(300):
            log.info(f"line {i} " + "x" * 80)
        rotated = sorted(p.name for p in self.root.glob("vc.log*"))
        self.assertIn("vc.log.1", rotated, rotated)
        self.assertLessEqual(len(rotated), 3, rotated)


class PickTodoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ResultStore(self.root / "results.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, size=10):
        p = self.root / name
        p.write_bytes(b"x" * size)
        return p

    def test_cached_files_are_dropped(self):
        a = self.write("a.mp4", 10)
        b = self.write("b.mp4", 10)
        self.store.add("ok", make_info(path=a, size=10), output=str(a),
                       new_size=10)
        self.assertEqual(cli._pick_todo([a, b], self.store, force=False), [b])

    def test_force_keeps_everything(self):
        a = self.write("a.mp4", 10)
        self.store.add("skipped", make_info(path=a, size=10), reason="tagged")
        self.assertEqual(cli._pick_todo([a], self.store, force=True), [a])


class MainEarlyExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = logging.getLogger().handlers[:]

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in self.saved:
            root.addHandler(h)
        self.tmp.cleanup()

    def write_cfg(self, body: str) -> Path:
        p = self.root / "config.toml"
        p.write_text(
            f'[run]\nlog_file = "{(self.root / "vc.log").as_posix()}"\n'
            f'results_file = "{(self.root / "r.jsonl").as_posix()}"\n' + body,
            encoding="utf-8")
        return p

    def test_missing_explicit_config_is_a_usage_error(self):
        rc = cli.main(["x.mp4", "--config", str(self.root / "nope.toml")])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_broken_config_is_a_usage_error(self):
        p = self.write_cfg("[skip]\nmin_saving_ration = 0.2\n")
        rc = cli.main(["x.mp4", "--config", str(p)])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_unknown_encoder_is_a_usage_error(self):
        p = self.write_cfg("")
        rc = cli.main(["x.mp4", "--config", str(p), "--encoder", "vp9"])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_no_matching_files_is_success(self):
        p = self.write_cfg("")
        rc = cli.main([str(self.root), "--config", str(p)])
        self.assertEqual(rc, cli.EXIT_OK)

    def test_all_paths_invalid_is_a_usage_error(self):
        """传入的路径全部失效多半是用法错误（比如把输出路径当了参数）。"""
        p = self.write_cfg("")
        rc = cli.main([str(self.root / "nope"), "--config", str(p)])
        self.assertEqual(rc, cli.EXIT_USAGE)

    def test_partially_invalid_paths_still_process_the_rest(self):
        vid = self.root / "a.mp4"
        vid.write_bytes(b"x")
        p = self.write_cfg("")
        with mock.patch.object(cli, "_run_serial") as run:
            rc = cli.main([str(self.root / "nope"), str(vid),
                           "--config", str(p)])
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(run.call_count, 1)


class MainShutdownTests(unittest.TestCase):
    """关机开关的端到端接线：开始时播报、结束时重读配置、关机前写回 false。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.saved = logging.getLogger().handlers[:]
        self.cfg_path = self.root / "config.toml"
        (self.root / "clip.mp4").write_bytes(b"x" * (30 * 1024 * 1024))
        self.commands = []

    def tearDown(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
            h.close()
        for h in self.saved:
            root.addHandler(h)
        self.tmp.cleanup()

    def write_cfg(self, flag: str) -> None:
        self.cfg_path.write_text(
            f'[run]\nlog_file = "{(self.root / "vc.log").as_posix()}"\n'
            f'results_file = "{(self.root / "r.jsonl").as_posix()}"\n'
            f'shutdown_when_done = {flag}\n'
            f'shutdown_delay_seconds = 0\n', encoding="utf-8")

    def run_main(self, extra=None):
        real_run = proc.run_managed

        def fake_run(cmd, cwd=None):
            if cmd and cmd[0] == "fake-shutdown":
                self.commands.append(cmd)
                return 0, "", ""
            return real_run(cmd, cwd=cwd)

        with mock.patch.object(power, "shutdown_command",
                               return_value=["fake-shutdown"]), \
                mock.patch.object(proc, "run_managed", fake_run):
            return cli.main([str(self.root), "--config", str(self.cfg_path)]
                            + (extra or []))

    def log_text(self) -> str:
        return (self.root / "vc.log").read_text(encoding="utf-8")

    def test_off_announces_how_to_turn_it_on_and_never_shuts_down(self):
        self.write_cfg("false")
        self.run_main()
        self.assertEqual(self.commands, [])
        self.assertIn("自动关机：关", self.log_text())
        self.assertIn("shutdown_when_done 改成 true", self.log_text())

    def test_flipping_the_flag_mid_run_still_takes_effect(self):
        self.write_cfg("false")
        real = cli._run_serial

        def flip(ctx, todo):
            self.write_cfg("true")  # 用户在压缩过程中改了配置
            return real(ctx, todo)

        with mock.patch.object(cli, "_run_serial", flip):
            self.run_main()
        self.assertEqual(self.commands, [["fake-shutdown"]])
        self.assertIn("shutdown_when_done = false",
                      self.cfg_path.read_text(encoding="utf-8"))
        self.assertIn("shutdown_when_done 写回 false", self.log_text())

    def test_dry_run_never_shuts_down(self):
        self.write_cfg("true")
        self.run_main(["--dry-run"])
        self.assertEqual(self.commands, [])
        self.assertIn("shutdown_when_done = true",
                      self.cfg_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
