import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest

from vcompress.util import (SIZE_UNITS, fmt_hms, fmt_size, fmt_size_change,
                            to_float, to_int)


class ToIntTests(unittest.TestCase):
    """ffprobe 会把整数写成 "6000000" 也会写成 "6000000.0"。"""

    def test_plain_int(self):
        self.assertEqual(to_int("6000000"), 6000000)

    def test_float_text(self):
        self.assertEqual(to_int("3.7"), 3)

    def test_garbage_falls_back(self):
        self.assertEqual(to_int(None), 0)
        self.assertEqual(to_int("N/A", 5), 5)


class ToFloatTests(unittest.TestCase):
    def test_parses(self):
        self.assertAlmostEqual(to_float("2.5"), 2.5)

    def test_garbage_falls_back(self):
        self.assertAlmostEqual(to_float("N/A", 1.5), 1.5)


class FmtHmsTests(unittest.TestCase):
    def test_hours_minutes_seconds(self):
        self.assertEqual(fmt_hms(3725), "1:02:05")

    def test_negative_is_zero(self):
        self.assertEqual(fmt_hms(-10), "0:00:00")


class FmtSizeTests(unittest.TestCase):
    """体积格式化只有这一份，各处日志的写法必须一致。"""

    def test_two_decimals_at_most(self):
        self.assertEqual(fmt_size(1544729575), "1.44 GB")
        self.assertEqual(fmt_size(1598395257), "1.49 GB")

    def test_trailing_zeros_are_dropped(self):
        """2.00 GB 写成 2 GB，2.10 写成 2.1——多余的零只是噪音。"""
        self.assertEqual(fmt_size(0), "0 B")
        self.assertEqual(fmt_size(512), "512 B")
        self.assertEqual(fmt_size(48 * 1024), "48 KB")
        self.assertEqual(fmt_size(2 * 1024**3), "2 GB")
        self.assertEqual(fmt_size(int(2.1 * 1024**3)), "2.1 GB")

    def test_integers_are_not_eaten_by_the_trim(self):
        """"100" 不能被当成小数尾零啃成 "1"。"""
        self.assertEqual(fmt_size(100), "100 B")
        self.assertEqual(fmt_size(1000 * 1024**2), "1000 MB")

    def test_scales_all_the_way_up(self):
        self.assertEqual(fmt_size(2 * 1024**4), "2 TB")
        self.assertEqual(fmt_size(3 * 1024**5), "3 PB")

    def test_beyond_the_last_unit_stays_in_it(self):
        """没有下一档了就继续用 PB 表示，不能掉出循环变成裸数字。"""
        self.assertTrue(fmt_size(5000 * 1024**5).endswith(f" {SIZE_UNITS[-1]}"))

    def test_two_decimals_keep_close_sizes_apart(self):
        """1.44 和 1.49 GB 必须区分得开——这正是判"产物变大了"要看的精度。"""
        self.assertNotEqual(fmt_size(1544729575), fmt_size(1598395257))

    def test_garbage_is_zero_rather_than_a_crash(self):
        self.assertEqual(fmt_size(None), "0 B")
        self.assertEqual(fmt_size("N/A"), "0 B")


class FmtSizeChangeTests(unittest.TestCase):
    def test_growth_gets_a_plus_sign(self):
        self.assertEqual(fmt_size_change(1544729575, 1598395257),
                         "1.44 GB -> 1.49 GB（+3.5%）")

    def test_shrink_gets_a_minus_sign(self):
        self.assertEqual(fmt_size_change(1000 * 1024**2, 750 * 1024**2),
                         "1000 MB -> 750 MB（-25.0%）")

    def test_same_size_reads_as_zero(self):
        self.assertEqual(fmt_size_change(1024, 1024), "1 KB -> 1 KB（+0.0%）")

    def test_indistinguishable_sizes_fall_back_to_exact_bytes(self):
        """差几百字节时 "1.44 GB -> 1.44 GB" 看起来像 bug，退回精确字节。"""
        self.assertEqual(fmt_size_change(1544729575, 1544729000),
                         "1544729575 字节 -> 1544729000 字节")

    def test_unknown_before_size_omits_the_percentage(self):
        """除不了就别硬算，只把两个数摆出来。"""
        self.assertEqual(fmt_size_change(0, 4096), "0 B -> 4 KB")
