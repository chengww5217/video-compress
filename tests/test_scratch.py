"""临时工作目录：无损样本片段放在哪里。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path

from vcompress import scratch


class ScratchDirTests(unittest.TestCase):
    """默认的系统临时目录常常是内存盘，几个 G 的无损样本放不下。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.orig = scratch.get_dir()

    def tearDown(self):
        scratch.set_dir(self.orig)
        self.tmp.cleanup()

    def test_set_dir_creates_it_and_temp_dirs_land_inside(self):
        target = self.root / "logs" / "tmp"
        self.assertEqual(scratch.set_dir(target), target)
        self.assertTrue(target.is_dir(), "父目录不存在也要建出来")
        with scratch.temp_dir(prefix="vctest_") as td:
            self.assertEqual(Path(td).parent, target)

    def test_empty_means_system_default(self):
        self.assertIsNone(scratch.set_dir(""))
        with scratch.temp_dir(prefix="vctest_") as td:
            self.assertEqual(Path(td).parent, Path(tempfile.gettempdir()))

    def test_directory_is_recreated_if_it_disappears(self):
        """一次运行可能跑几十小时，中间被清理工具扫掉也得能继续。"""
        target = self.root / "scratch"
        scratch.set_dir(target)
        target.rmdir()
        with scratch.temp_dir(prefix="vctest_") as td:
            self.assertEqual(Path(td).parent, target)

    def test_unusable_directory_falls_back_to_system_temp(self):
        """建不出来时退回系统临时目录：总比整个文件失败好。"""
        blocker = self.root / "not-a-dir"
        blocker.write_bytes(b"x")
        self.assertIsNone(scratch.set_dir(blocker / "sub"))
        with scratch.temp_dir(prefix="vctest_") as td:
            self.assertEqual(Path(td).parent, Path(tempfile.gettempdir()))

    def test_temp_dir_falls_back_when_the_target_turns_into_a_file(self):
        target = self.root / "scratch2"
        scratch.set_dir(target)
        target.rmdir()
        target.write_bytes(b"x")  # 位置被别的东西占了
        with scratch.temp_dir(prefix="vctest_") as td:
            self.assertEqual(Path(td).parent, Path(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
