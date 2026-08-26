import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import re
import tempfile
import unittest
from unittest import mock

from vcompress import logfmt


def record(level=logging.INFO, msg="消息"):
    return logging.LogRecord("vcompress", level, __file__, 1, msg, None, None)


class MarkerTests(unittest.TestCase):
    def test_em_wraps_and_strip_removes(self):
        marked = logfmt.em("a.mkv")
        self.assertIn("a.mkv", marked)
        self.assertNotEqual(marked, "a.mkv")
        self.assertEqual(logfmt.strip_marks(marked), "a.mkv")


class ConsoleFormatterTests(unittest.TestCase):
    def fmt(self, level, msg, color=True):
        return logfmt.ConsoleFormatter(color).format(record(level, msg))

    def test_plain_mode_matches_the_file_format_exactly(self):
        """控制台与文件共用同一份布局：剥掉颜色转义后逐字节一致。"""
        rec = record(logging.WARNING, f"压缩 {logfmt.em('a.mkv')}")
        self.assertEqual(logfmt.ConsoleFormatter(False).format(rec),
                         logfmt.FileFormatter().format(rec))

    def test_plain_mode_has_no_escape_codes_or_markers(self):
        out = self.fmt(logging.INFO, f"压缩 {logfmt.em('a.mkv')}", color=False)
        self.assertRegex(out, r"^\d{4} \d{2}:\d{2}:\d{2} INFO\s+压缩 a\.mkv$")
        self.assertNotIn("\x1b", out)
        self.assertNotIn(logfmt.EM_START, out)

    def test_colored_mode_highlights_and_leaves_no_markers(self):
        out = self.fmt(logging.INFO, f"压缩 {logfmt.em('a.mkv')}")
        self.assertIn("\x1b[", out)
        self.assertNotIn(logfmt.EM_START, out)
        self.assertNotIn(logfmt.EM_END, out)
        self.assertIn("a.mkv", out)

    def test_colored_mode_keeps_the_file_layout_underneath(self):
        out = self.fmt(logging.WARNING, "小心")
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        self.assertRegex(plain, r"^\d{4} \d{2}:\d{2}:\d{2} WARNING 小心$")

    def test_levels_use_different_colors(self):
        warn = self.fmt(logging.WARNING, "小心")
        err = self.fmt(logging.ERROR, "炸了")
        self.assertIn(logfmt.YELLOW, warn)
        self.assertIn("WARNING", warn)
        self.assertIn(logfmt.RED, err)
        self.assertIn("ERROR", err)

    def test_status_tag_is_colored_separately(self):
        out = self.fmt(logging.INFO, "[完成] a.mkv：省下 50%")
        self.assertIn(logfmt.GREEN, out)
        self.assertIn("[完成]", out)

    def test_an_unknown_tag_is_left_alone(self):
        out = self.fmt(logging.INFO, "[什么] a.mkv", color=False)
        self.assertIn("[什么] a.mkv", out)

    def test_exception_text_is_kept(self):
        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.LogRecord("vcompress", logging.ERROR, __file__, 1,
                                    "出错", None, sys.exc_info())
        out = logfmt.ConsoleFormatter(False).format(rec)
        self.assertIn("ValueError: boom", out)


class FileFormatterTests(unittest.TestCase):
    def test_no_color_no_markers_but_has_a_date(self):
        out = logfmt.FileFormatter().format(
            record(logging.WARNING, f"清掉 {logfmt.em('a.tmp.mkv')}"))
        self.assertNotIn("\x1b", out)
        self.assertNotIn(logfmt.EM_START, out)
        self.assertIn("WARNING", out)
        self.assertIn("清掉 a.tmp.mkv", out)
        # 月日 时:分:秒
        self.assertRegex(out, r"^\d{4} \d{2}:\d{2}:\d{2} ")


class ColorDetectionTests(unittest.TestCase):
    class _Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    def test_not_a_tty_means_no_color(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(logfmt.supports_color(self._Stream(False)))

    def test_a_tty_gets_color(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("os.name", "posix"):
            self.assertTrue(logfmt.supports_color(self._Stream(True)))

    def test_no_color_env_wins(self):
        with mock.patch.dict("os.environ", {"NO_COLOR": "1"}, clear=True):
            self.assertFalse(logfmt.supports_color(self._Stream(True)))

    def test_force_color_env_wins(self):
        with mock.patch.dict("os.environ", {"FORCE_COLOR": "1"}, clear=True):
            self.assertTrue(logfmt.supports_color(self._Stream(False)))

    def test_dumb_terminal_gets_no_color(self):
        with mock.patch.dict("os.environ", {"TERM": "dumb"}, clear=True):
            self.assertFalse(logfmt.supports_color(self._Stream(True)))


class SessionBannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "vc.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_banner_is_appended_with_blank_lines(self):
        self.log.write_text("旧的一行\n", encoding="utf-8")
        logfmt.write_session_banner(self.log, ["vcompress", "D:\\videos"])
        text = self.log.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("旧的一行\n"))
        self.assertIn("\n\n\n====", text)
        self.assertIn("启动程序 ====", text)
        self.assertIn("命令行: vcompress D:\\videos", text)
        self.assertRegex(text, r"==== \d{4} \d{2}:\d{2}:\d{2} 启动程序 ====")

    def test_an_unwritable_path_does_not_raise(self):
        logfmt.write_session_banner(Path(self.tmp.name) / "nope" / "vc.log")


if __name__ == "__main__":
    unittest.main()
