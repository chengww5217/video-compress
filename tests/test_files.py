import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import errno
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers
from vcompress import files
from vcompress.files import (OutputRegistry, clean_stale_tmp, gather_files,
                             keep_failed_output, move_file, output_name,
                             resolve_final_path, tmp_path)


class OutputNameTests(unittest.TestCase):
    """默认输出名必须带编码器后缀：a.mp4 -> a.av1.mkv。"""

    def test_mp4_source(self):
        self.assertEqual(output_name(Path("/v/a.mp4"), "av1", ".mkv"),
                         Path("/v/a.av1.mkv"))

    def test_mkv_source_gets_a_distinct_name(self):
        """同容器转码（a.mkv -> a.av1.mkv）不再原地替换，避免误删原文件。"""
        self.assertEqual(output_name(Path("/v/a.mkv"), "av1", ".mkv"),
                         Path("/v/a.av1.mkv"))

    def test_multi_dot_name_keeps_the_stem(self):
        self.assertEqual(output_name(Path("/v/movie.2024.mp4"), "av1", ".mkv"),
                         Path("/v/movie.2024.av1.mkv"))

    def test_other_encoder_name_is_used(self):
        self.assertEqual(output_name(Path("/v/a.mp4"), "x265", ".mkv"),
                         Path("/v/a.x265.mkv"))


class TmpNamingTests(unittest.TestCase):
    """临时文件名必须带上源文件的完整名字，否则同 stem 的源会互相覆盖。"""

    def test_name_includes_the_source_extension(self):
        self.assertEqual(tmp_path(Path("/v/a.mp4"), ".mkv"),
                         Path("/v/a.mp4.tmp.mkv"))

    def test_same_stem_different_container_never_collide(self):
        a = tmp_path(Path("/v/a.mp4"), ".mkv")
        b = tmp_path(Path("/v/a.avi"), ".mkv")
        c = tmp_path(Path("/v/a.mkv"), ".mkv")
        self.assertEqual(len({a, b, c}), 3, f"{a}, {b}, {c} must differ")

    def test_failed_name_also_carries_the_source_name(self):
        self.assertEqual(files.failed_path(Path("/v/a.mp4"), ".mkv"),
                         Path("/v/a.mp4.failed.mkv"))

    def test_own_artefacts_are_recognised(self):
        self.assertTrue(files.is_intermediate("a.mp4.tmp.mkv"))
        self.assertTrue(files.is_intermediate("a.mp4.failed.mkv"))
        self.assertFalse(files.is_intermediate("a.mkv"))
        self.assertFalse(files.is_intermediate("a.av1.mkv"))


class GatherFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "a.mp4").write_bytes(b"x")
        (root / "e.mp4.tmp.mkv").write_bytes(b"x")
        (root / "f.mp4.failed.mkv").write_bytes(b"x")
        (root / "note.txt").write_bytes(b"x")
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "sub" / "c.mkv").write_bytes(b"x")
        (root / "sub" / "deep" / "d.mp4").write_bytes(b"x")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, found):
        return {f.name for f in found}

    def gather(self, paths, recursive, extensions):
        return gather_files(paths, recursive, extensions)[0]

    def test_recursive_finds_and_filters(self):
        found = self.gather([self.root], True, {".mp4", ".mkv"})
        self.assertEqual(self.names(found), {"a.mp4", "c.mkv", "d.mp4"})

    def test_own_intermediate_files_are_never_inputs(self):
        found = self.gather([self.root], True, {".mp4", ".mkv"})
        self.assertNotIn("e.mp4.tmp.mkv", self.names(found))
        self.assertNotIn("f.mp4.failed.mkv", self.names(found),
                         "rejected outputs must not be re-compressed")

    def test_no_recursive_only_top_level(self):
        found = self.gather([self.root], False, {".mp4", ".mkv"})
        self.assertEqual(self.names(found), {"a.mp4"})

    def test_deduplicates_overlapping_paths(self):
        dup, invalid = gather_files([self.root, self.root], True,
                                    {".mp4", ".mkv"})
        self.assertEqual(len(dup), 3)
        self.assertEqual(invalid, [])

    def test_explicit_file_accepted(self):
        f = self.root / "a.mp4"
        found = self.gather([f, f], False, {".mp4"})
        self.assertEqual(len(found), 1)

    def test_missing_path_warns_instead_of_vanishing(self):
        """不存在的路径必须留痕：用户可能把输出路径当成了第二个参数。"""
        ghost = self.root / "ghost.mp4"
        with self.assertLogs("vcompress", level="WARNING") as cm:
            found, invalid = gather_files([ghost, self.root / "a.mp4"],
                                          False, {".mp4"})
        self.assertEqual(self.names(found), {"a.mp4"})
        self.assertEqual(invalid, [ghost])
        self.assertTrue(any("ghost.mp4" in line for line in cm.output))

    def test_explicit_unsupported_extension_warns(self):
        with self.assertLogs("vcompress", level="WARNING") as cm:
            found, invalid = gather_files([self.root / "note.txt"],
                                          False, {".mp4"})
        self.assertEqual(found, [])
        self.assertEqual(invalid, [], "存在的文件只是类型不符，不算失效路径")
        self.assertTrue(any("note.txt" in line for line in cm.output))

    def test_explicit_intermediate_file_warns(self):
        """目录扫描静默排除中途产物是对的，显式点名则被跳过必须留痕。"""
        f = self.root / "e.mp4.tmp.mkv"
        with self.assertLogs("vcompress", level="WARNING") as cm:
            found = self.gather([f], False, {".mp4", ".mkv"})
        self.assertEqual(found, [])
        self.assertTrue(any("e.mp4.tmp.mkv" in line for line in cm.output))

    def test_empty_directory_warns(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertLogs("vcompress", level="WARNING") as cm:
            found = self.gather([empty], True, {".mp4"})
        self.assertEqual(found, [])
        self.assertTrue(any("empty" in line for line in cm.output))


class SortFilesTests(unittest.TestCase):
    """处理顺序：默认按路径名，也可以按体积、修改时间或传入顺序。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, size=1, mtime=None):
        p = self.root / name
        p.write_bytes(b"x" * size)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_name_sorts_by_path(self):
        c = self.write("c.mp4")
        a = self.write("a.mp4")
        self.assertEqual(files.sort_files([c, a], "name"), [a, c])

    def test_name_desc_is_the_exact_reverse(self):
        c = self.write("c.mp4")
        a = self.write("a.mp4")
        b = self.write("b.mp4")
        self.assertEqual(files.sort_files([a, c, b], "name-desc"), [c, b, a])

    def test_size_small_files_first(self):
        big = self.write("big.mp4", 300)
        mid = self.write("mid.mp4", 200)
        small = self.write("small.mp4", 100)
        self.assertEqual(files.sort_files([big, mid, small], "size"),
                         [small, mid, big])

    def test_size_desc_big_files_first(self):
        big = self.write("big.mp4", 300)
        mid = self.write("mid.mp4", 200)
        small = self.write("small.mp4", 100)
        self.assertEqual(files.sort_files([big, mid, small], "size-desc"),
                         [big, mid, small])

    def test_mtime_oldest_first(self):
        new = self.write("new.mp4", mtime=2_000_000_000)
        old = self.write("old.mp4", mtime=1_000_000_000)
        self.assertEqual(files.sort_files([new, old], "mtime"), [old, new])

    def test_mtime_desc_newest_first(self):
        new = self.write("new.mp4", mtime=2_000_000_000)
        old = self.write("old.mp4", mtime=1_000_000_000)
        self.assertEqual(files.sort_files([new, old], "mtime-desc"), [new, old])

    def test_none_keeps_the_given_order(self):
        b = self.write("b.mp4")
        a = self.write("a.mp4")
        self.assertEqual(files.sort_files([b, a], "none"), [b, a])

    def test_ties_fall_back_to_the_path(self):
        """属性值并列时按路径分先后：顺序仍然是确定的，方向反转也不乱。"""
        y = self.write("y.mp4", 100)
        x = self.write("x.mp4", 100)
        self.assertEqual(files.sort_files([y, x], "size"), [x, y])
        self.assertEqual(files.sort_files([y, x], "size-desc"), [x, y])

    def test_unstatable_files_sort_last_in_both_directions(self):
        """排序时文件刚好被删：排序本身不许炸，怎么排都放最后。"""

        class Ghost:
            def stat(self):
                raise OSError("gone")

            def __str__(self):
                return "/gone/z.mp4"

        ghost = Ghost()
        a = self.write("a.mp4", 100)
        self.assertIs(files.sort_files([ghost, a], "size")[-1], ghost)
        self.assertIs(files.sort_files([ghost, a], "size-desc")[-1], ghost)

    def test_unknown_order_is_rejected(self):
        with self.assertRaises(ValueError):
            files.sort_files([], "biggest")

    def test_gather_files_applies_the_sort(self):
        """gather_files 收完就排好序：size 和默认的 name 得到不同顺序。"""
        b = self.write("b.mp4", 100)
        a = self.write("a.mp4", 300)
        found, _ = gather_files([b, a], False, {".mp4"}, "size")
        self.assertEqual(found, [b, a])
        found, _ = gather_files([b, a], False, {".mp4"})
        self.assertEqual(found, [a, b])


class CleanStaleTmpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name):
        p = self.root / name
        p.write_bytes(b"x")
        return p

    def test_removes_the_temp_file_of_a_pending_source(self):
        src = self.touch("a.mp4")
        stale = self.touch("a.mp4.tmp.mkv")
        self.assertEqual(clean_stale_tmp([src], ".mkv"), 1)
        self.assertFalse(stale.exists())
        self.assertTrue(src.exists())

    def test_keeps_temp_files_of_untouched_sources(self):
        src = self.touch("a.mp4")
        mine = self.touch("a.mp4.tmp.mkv")
        someone_else = self.touch("holiday.mp4.tmp.mkv")
        self.assertEqual(clean_stale_tmp([src], ".mkv"), 1)
        self.assertFalse(mine.exists())
        self.assertTrue(someone_else.exists(),
                        "unrelated temp files must be left alone")

    def test_sibling_source_temp_is_left_alone(self):
        """a.mp4 的清理不能碰 a.avi 的临时文件。"""
        src = self.touch("a.mp4")
        other = self.touch("a.avi.tmp.mkv")
        self.assertEqual(clean_stale_tmp([src], ".mkv"), 0)
        self.assertTrue(other.exists())

    def test_other_suffixes_untouched(self):
        src = self.touch("a.mp4")
        other = self.touch("a.mp4.tmp.txt")
        self.assertEqual(clean_stale_tmp([src], ".mkv"), 0)
        self.assertTrue(other.exists())

    def test_nothing_to_clean(self):
        src = self.touch("a.mp4")
        self.assertEqual(clean_stale_tmp([src], ".mkv"), 0)


class ResolveFinalPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name):
        p = self.root / name
        p.write_bytes(b"x")
        return p

    def test_free_target_used_as_is(self):
        src = self.touch("a.mp4")
        target = self.root / "a.mkv"
        self.assertEqual(
            resolve_final_path(src, target, ".mkv", "av1", keep=False), target)

    def test_in_place_replacement_allowed(self):
        src = self.touch("a.mkv")
        self.assertEqual(
            resolve_final_path(src, src, ".mkv", "av1", keep=False), src)

    def test_unrelated_target_is_not_overwritten(self):
        src = self.touch("a.mp4")
        victim = self.touch("a.mkv")
        final = resolve_final_path(src, victim, ".mkv", "av1", keep=False)
        self.assertEqual(final, self.root / "a.av1.mkv")
        self.assertTrue(victim.exists())

    def test_keep_mode_never_targets_source(self):
        src = self.touch("a.mkv")
        self.assertEqual(resolve_final_path(src, src, ".mkv", "av1", keep=True),
                         self.root / "a.av1.mkv")

    def test_suffix_target_falls_back_to_numbered_name(self):
        """默认名（a.av1.mkv）被占时按 -2、-3…让位，不能拼出双后缀。"""
        src = self.touch("a.mp4")
        self.touch("a.av1.mkv")
        final = resolve_final_path(src, self.root / "a.av1.mkv", ".mkv",
                                   "av1", keep=False)
        self.assertEqual(final, self.root / "a.av1-2.mkv")

    def test_numbered_fallback_skips_taken_names(self):
        src = self.touch("a.mp4")
        self.touch("a.av1.mkv")
        self.touch("a.av1-2.mkv")
        final = resolve_final_path(src, self.root / "a.av1.mkv", ".mkv",
                                   "av1", keep=False)
        self.assertEqual(final, self.root / "a.av1-3.mkv")

    def test_reserved_target_is_treated_as_taken(self):
        """磁盘上还不存在但已被别的文件预定的名字同样不能用。"""
        src = self.touch("a.mp4")
        target = self.root / "a.mkv"
        final = resolve_final_path(src, target, ".mkv", "av1", keep=False,
                                   taken=[target])
        self.assertEqual(final, self.root / "a.av1.mkv")

    def test_fallback_stays_in_the_targets_directory(self):
        """输出目录（-o）模式下备用名必须落在输出目录，不是源文件旁边。"""
        src = self.touch("a.mp4")
        out = self.root / "out"
        out.mkdir()
        (out / "a.mkv").write_bytes(b"x")
        final = resolve_final_path(src, out / "a.mkv", ".mkv", "av1",
                                   keep=False)
        self.assertEqual(final, out / "a.av1.mkv")


class MoveFileTests(unittest.TestCase):
    """跨盘搬迁：os.replace 过不去就退化为复制后删除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_volume_move(self):
        src = self.root / "a.tmp.mkv"
        src.write_bytes(b"payload")
        dst = self.root / "a.mkv"
        move_file(src, dst)
        self.assertEqual(dst.read_bytes(), b"payload")
        self.assertFalse(src.exists())

    def test_cross_device_falls_back_to_copy(self):
        src = self.root / "a.tmp.mkv"
        src.write_bytes(b"payload")
        dst = self.root / "a.mkv"
        boom = OSError(errno.EXDEV, "Invalid cross-device link")
        with mock.patch("vcompress.files.os.replace", side_effect=boom):
            move_file(src, dst)
        self.assertEqual(dst.read_bytes(), b"payload")
        self.assertFalse(src.exists())

    def test_unrelated_errors_are_not_silently_copied_over(self):
        src = self.root / "a.tmp.mkv"
        src.write_bytes(b"payload")
        boom = OSError(errno.EACCES, "Permission denied")
        with mock.patch("vcompress.files.os.replace", side_effect=boom):
            with self.assertRaises(OSError):
                move_file(src, self.root / "a.mkv")


class OutputRegistryTests(unittest.TestCase):
    """并发时两个源文件不能抢到同一个输出名。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.reg = OutputRegistry()

    def tearDown(self):
        self.tmp.cleanup()

    def touch(self, name):
        p = self.root / name
        p.write_bytes(b"x")
        return p

    def claim(self, src):
        return self.reg.claim(src, src.with_suffix(".mkv"), ".mkv", "av1",
                              keep=False)

    def test_same_stem_sources_get_different_outputs(self):
        a = self.touch("a.mp4")
        b = self.touch("a.avi")
        first = self.claim(a)
        second = self.claim(b)
        self.assertEqual(first, self.root / "a.mkv")
        self.assertEqual(second, self.root / "a.av1.mkv")

    def test_third_collision_gets_a_numbered_name(self):
        for name in ("a.mp4", "a.avi", "a.ts"):
            self.touch(name)
        first = self.claim(self.root / "a.mp4")
        second = self.claim(self.root / "a.avi")
        third = self.claim(self.root / "a.ts")
        self.assertEqual(first, self.root / "a.mkv")
        self.assertEqual(second, self.root / "a.av1.mkv")
        self.assertEqual(third, self.root / "a.av1-2.mkv",
                         "never overwrite; fall back to -2, -3…")

    def test_default_names_collide_and_fall_back(self):
        """默认名都是 a.av1.mkv 的两个源，第二个必须让位给 a.av1-2.mkv。"""
        a = self.touch("a.mp4")
        b = self.touch("a.mkv")
        first = self.reg.claim(a, output_name(a, "av1", ".mkv"), ".mkv",
                               "av1", keep=False)
        second = self.reg.claim(b, output_name(b, "av1", ".mkv"), ".mkv",
                                "av1", keep=False)
        self.assertEqual(first, self.root / "a.av1.mkv")
        self.assertEqual(second, self.root / "a.av1-2.mkv")

    def test_reclaiming_from_the_same_owner_is_stable(self):
        """同一个文件编码前后各定名一次，结果必须一致。"""
        a = self.touch("a.mp4")
        self.assertEqual(self.claim(a), self.claim(a))

    def test_release_frees_the_name(self):
        a = self.touch("a.mp4")
        b = self.touch("a.avi")
        self.assertEqual(self.claim(a), self.root / "a.mkv")
        self.reg.release(a)
        self.assertEqual(self.claim(b), self.root / "a.mkv")

    def test_in_place_source_keeps_its_own_name(self):
        mkv = self.touch("a.mkv")
        avi = self.touch("a.avi")
        self.assertEqual(self.claim(mkv), mkv)
        self.assertEqual(self.claim(avi), self.root / "a.av1.mkv")

    def test_concurrent_claims_are_all_distinct(self):
        sources = [self.touch(f"clip{i}.mp4") for i in range(8)]
        # 全部指向同一个目标名，考验锁
        target = self.root / "same.mkv"
        results = []
        lock = threading.Lock()

        def worker(src):
            try:
                got = self.reg.claim(src, target, ".mkv", "av1", keep=False)
            except RuntimeError:
                got = None
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker, args=(s,)) for s in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        granted = [r for r in results if r is not None]
        self.assertEqual(len(granted), len(set(granted)),
                         "two files were handed the same output path")


class KeepFailedOutputTests(unittest.TestCase):
    """校验没过时可以留下产物，供人工确认到底是画质坏了还是量错了。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "a.mp4"
        self.src.write_bytes(b"src")
        self.tmp_out = files.tmp_path(self.src, ".mkv")
        self.tmp_out.write_bytes(b"encoded")

    def tearDown(self):
        self.tmp.cleanup()

    def test_renamed_next_to_the_source(self):
        kept = keep_failed_output(self.tmp_out, self.src, ".mkv")
        self.assertEqual(kept, self.root / "a.mp4.failed.mkv")
        self.assertEqual(kept.read_bytes(), b"encoded")
        self.assertFalse(self.tmp_out.exists())
        self.assertTrue(self.src.exists(), "original must survive")

    def test_existing_name_is_never_overwritten(self):
        victim = self.root / "a.mp4.failed.mkv"
        victim.write_bytes(b"older attempt")
        self.assertIsNone(keep_failed_output(self.tmp_out, self.src, ".mkv"))
        self.assertEqual(victim.read_bytes(), b"older attempt")

    def test_missing_temp_file_is_a_no_op(self):
        self.tmp_out.unlink()
        self.assertIsNone(keep_failed_output(self.tmp_out, self.src, ".mkv"))


if __name__ == "__main__":
    unittest.main()
