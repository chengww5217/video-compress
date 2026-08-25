import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import tempfile
import unittest
from unittest import mock

from vcompress import config, logfmt, power


class _Capture(logging.Handler):
    def __init__(self, sink, level=logging.DEBUG):
        super().__init__(level=level)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record.getMessage())


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = []
        self.handler = _Capture(self.logs)
        self.log = logging.getLogger("vcompress")
        self.saved_level = self.log.level
        self.log.setLevel(logging.DEBUG)
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.log.removeHandler(self.handler)
        self.log.setLevel(self.saved_level)
        self.tmp.cleanup()

    def messages(self):
        return logfmt.strip_marks("\n".join(self.logs))

    def write_cfg(self, body: str) -> Path:
        p = self.root / "config.toml"
        p.write_text(body, encoding="utf-8")
        return p


class ConfigDefaultTests(unittest.TestCase):
    def test_off_by_default(self):
        cfg = config.normalise({})
        self.assertFalse(cfg["run"]["shutdown_when_done"])
        self.assertEqual(cfg["run"]["shutdown_delay_seconds"], 60)

    def test_wrong_type_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.normalise({"run": {"shutdown_when_done": "yes"}})

    def test_negative_delay_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.normalise({"run": {"shutdown_delay_seconds": -1}})


class ReadFlagTests(_Base):
    def test_true(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        self.assertTrue(power.read_flag(p))
        self.assertIn("shutdown_when_done = true", self.messages())

    def test_false(self):
        p = self.write_cfg("[run]\nshutdown_when_done = false\n")
        self.assertFalse(power.read_flag(p))

    def test_key_absent(self):
        p = self.write_cfg("[run]\nworkers = 2\n")
        self.assertFalse(power.read_flag(p))

    def test_missing_file(self):
        self.assertFalse(power.read_flag(self.root / "nope.toml"))

    def test_broken_toml_does_not_raise(self):
        p = self.write_cfg("[run\nshutdown_when_done = true\n")
        self.assertFalse(power.read_flag(p))
        self.assertIn("重读", self.messages())

    def test_non_boolean_is_refused(self):
        p = self.write_cfg('[run]\nshutdown_when_done = "true"\n')
        self.assertFalse(power.read_flag(p))
        self.assertIn("不是布尔值", self.messages())

    def test_the_flag_is_read_from_disk_not_from_the_loaded_config(self):
        """跑到一半改配置必须生效，这是整个功能的前提。"""
        p = self.write_cfg("[run]\nshutdown_when_done = false\n")
        cfg = config.load(p)
        self.assertFalse(cfg["run"]["shutdown_when_done"])
        p.write_text("[run]\nshutdown_when_done = true\n", encoding="utf-8")
        self.assertTrue(power.read_flag(p))


class DisarmTests(_Base):
    def test_only_the_value_changes(self):
        p = self.write_cfg(
            "# head\n[run]\nworkers = 2\n"
            "shutdown_when_done = true  # 关机开关\n"
            "shutdown_delay_seconds = 30\n")
        self.assertTrue(power.disarm(p))
        self.assertEqual(
            p.read_text(encoding="utf-8"),
            "# head\n[run]\nworkers = 2\n"
            "shutdown_when_done = false  # 关机开关\n"
            "shutdown_delay_seconds = 30\n")
        self.assertFalse(power.read_flag(p))

    def test_missing_key_is_a_failure(self):
        p = self.write_cfg("[run]\nworkers = 2\n")
        self.assertFalse(power.disarm(p))
        self.assertIn("找不到", self.messages())

    def test_a_key_in_another_section_is_left_alone(self):
        p = self.write_cfg(
            "[skip]\nshutdown_when_done = true\n[run]\nworkers = 1\n")
        self.assertFalse(power.disarm(p))
        self.assertIn("shutdown_when_done = true", p.read_text(encoding="utf-8"))

    def test_no_trailing_newline(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true")
        self.assertTrue(power.disarm(p))
        self.assertEqual(p.read_text(encoding="utf-8"),
                         "[run]\nshutdown_when_done = false")

    def test_the_file_survives_a_write_failure(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        before = p.read_text(encoding="utf-8")
        with mock.patch.object(Path, "write_text",
                               side_effect=OSError("disk full")):
            self.assertFalse(power.disarm(p))
        self.assertEqual(p.read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / "config.toml.tmp").exists())


class ShutdownCommandTests(unittest.TestCase):
    def cmd_for(self, system, which=None):
        with mock.patch("platform.system", return_value=system), \
                mock.patch("shutil.which", return_value=which):
            return power.shutdown_command()

    def test_windows(self):
        self.assertEqual(self.cmd_for("Windows"), ["shutdown", "/s", "/t", "0"])

    def test_macos(self):
        cmd = self.cmd_for("Darwin")
        self.assertEqual(cmd[0], "osascript")
        self.assertIn("shut down", cmd[-1])

    def test_linux_prefers_systemctl(self):
        self.assertEqual(self.cmd_for("Linux", which="/bin/systemctl"),
                         ["systemctl", "poweroff"])

    def test_linux_without_systemctl(self):
        self.assertEqual(self.cmd_for("Linux"), ["shutdown", "-h", "now"])

    def test_unknown_platform(self):
        self.assertIsNone(self.cmd_for("Plan9"))


class MaybeShutdownTests(_Base):
    def setUp(self):
        super().setUp()
        self.calls = []

    def runner(self, code=0, err=""):
        def run(cmd, cwd=None):
            self.calls.append(cmd)
            return code, "", err
        return run

    def test_off_does_nothing(self):
        p = self.write_cfg("[run]\nshutdown_when_done = false\n")
        self.assertFalse(power.maybe_shutdown(p, 0, runner=self.runner()))
        self.assertEqual(self.calls, [])
        self.assertIn("机器保持运行", self.messages())

    def test_on_disarms_first_then_shuts_down(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        with mock.patch("platform.system", return_value="Windows"):
            self.assertTrue(power.maybe_shutdown(p, 0, runner=self.runner()))
        self.assertEqual(self.calls, [["shutdown", "/s", "/t", "0"]])
        self.assertIn("shutdown_when_done = false",
                      p.read_text(encoding="utf-8"))

    def test_a_failed_disarm_cancels_the_shutdown(self):
        """改不回 false 就不能关机，否则下次运行还会关。"""
        p = self.write_cfg("[run]\nworkers = 1\n")
        p.write_text("[run]\nworkers = 1\n", encoding="utf-8")
        with mock.patch.object(power, "read_flag", return_value=True):
            self.assertFalse(power.maybe_shutdown(p, 0, runner=self.runner()))
        self.assertEqual(self.calls, [])

    def test_a_failing_command_is_reported(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        with mock.patch("platform.system", return_value="Windows"):
            ok = power.maybe_shutdown(
                p, 0, runner=self.runner(code=1, err="access denied"))
        self.assertFalse(ok)
        self.assertIn("access denied", self.messages())

    def test_unknown_platform_still_disarms(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        with mock.patch("platform.system", return_value="Plan9"):
            self.assertFalse(power.maybe_shutdown(p, 0, runner=self.runner()))
        self.assertFalse(power.read_flag(p))

    def test_countdown_can_be_cancelled(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        with mock.patch("platform.system", return_value="Windows"), \
                mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            self.assertFalse(power.maybe_shutdown(p, 5, runner=self.runner()))
        self.assertEqual(self.calls, [])
        self.assertIn("已取消自动关机", self.messages())


class AnnounceTests(_Base):
    def test_off_tells_the_user_how_to_turn_it_on(self):
        p = self.write_cfg("[run]\nshutdown_when_done = false\n")
        power.announce(p, False, 60)
        msg = self.messages()
        self.assertIn("自动关机：关", msg)
        self.assertIn("shutdown_when_done 改成 true", msg)
        self.assertIn(str(p), msg)

    def test_on_says_how_to_cancel(self):
        p = self.write_cfg("[run]\nshutdown_when_done = true\n")
        power.announce(p, True, 30)
        msg = self.messages()
        self.assertIn("自动关机：开", msg)
        self.assertIn("30 秒", msg)
        self.assertIn("shutdown_when_done 改回 false", msg)

    def test_without_a_config_file(self):
        power.announce(self.root / "nope.toml", False, 60)
        self.assertIn("不存在", self.messages())


if __name__ == "__main__":
    unittest.main()
