"""子进程注册表：取消守卫与错误信息提取。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

from vcompress import proc


class CancelGuardTests(unittest.TestCase):
    """取消之后不能再起子进程。

    实际踩过的坑：Ctrl+C 打断了自适应搜索的样本编码，搜索把它当成「搜索
    失败」回退到固定 CRF，紧接着起了一个全新的、要跑几小时的编码——汇总都
    已经打完了。terminate_all 只杀得掉已经在跑的，拦不住之后才起的。
    """

    def tearDown(self):
        proc.clear_cancel()

    def test_spawn_refuses_after_cancel(self):
        proc.terminate_all()
        with self.assertRaises(proc.Cancelled):
            proc.spawn([sys.executable, "-c", "pass"])

    def test_run_managed_refuses_after_cancel(self):
        proc.terminate_all()
        with self.assertRaises(proc.Cancelled):
            proc.run_managed([sys.executable, "-c", "pass"])

    def test_cancelled_is_a_keyboard_interrupt(self):
        """让所有已经在处理 Ctrl+C 的地方自动认得它，不用各处补一个分支。"""
        self.assertTrue(issubclass(proc.Cancelled, KeyboardInterrupt))
        self.assertFalse(issubclass(proc.Cancelled, Exception))

    def test_clear_cancel_lets_spawn_work_again(self):
        proc.terminate_all()
        proc.clear_cancel()
        rc, _out, _err = proc.run_managed([sys.executable, "-c", "pass"])
        self.assertEqual(rc, 0)


class ErrorTailTests(unittest.TestCase):
    """ffmpeg 的失败原因常被进度行和收尾统计挤出窗口。"""

    def test_cause_survives_a_wall_of_encoder_stats(self):
        """真实案例：磁盘写满的提示被 x264 的统计顶掉，只剩 Conversion failed!"""
        err = "\n".join([
            "frame=  144 fps= 22 q=31.0 size=    1KiB time=00:00:04.77 "
            "bitrate=   0.9kbits/s speed=0.734x",
            "av_interleaved_write_frame(): No space left on device",
            "[libx264 @ 0x1] frame I:1     Avg QP:12.00  size:123456",
            "[libx264 @ 0x1] coded y,uvDC,uvAC intra: 99.4% 75.0% 74.0%",
            "[libx264 @ 0x1] i16 v,h,dc,p: 71% 29%  1%  0%",
            "[libx264 @ 0x1] kb/s:497611.09",
            "Conversion failed!",
        ])
        tail = proc.error_tail(err)
        self.assertIn("No space left on device", tail)

    def test_progress_lines_are_dropped(self):
        err = ("frame=   33 fps=6.0 q=12.0 size=  1KiB speed=0.194x\n"
               "size=     512KiB time=00:00:05.33 bitrate= 785.6kbits/s\n"
               "Invalid data found when processing input")
        self.assertEqual(proc.error_tail(err),
                         "Invalid data found when processing input")

    def test_result_is_one_line(self):
        """多行塞进日志会让一条记录跨行，按行解析的日志全乱。"""
        err = "Error one\nError two\nError three"
        tail = proc.error_tail(err)
        self.assertNotIn("\n", tail)
        self.assertIn("Error three", tail)

    def test_falls_back_to_the_plain_tail_without_error_keywords(self):
        err = "something odd happened\nand then it stopped"
        self.assertIn("and then it stopped", proc.error_tail(err))

    def test_length_is_capped(self):
        self.assertLessEqual(len(proc.error_tail("error " + "x" * 900)), 300)

    def test_empty_input(self):
        self.assertEqual(proc.error_tail(""), "")
        self.assertEqual(proc.error_tail(None), "")


if __name__ == "__main__":
    unittest.main()
