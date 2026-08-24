import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import make_cfg, make_info
from vcompress.cli import (ResultStore, clean_stale_tmp, gather_files,
                           resolve_final_path)


class ResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "results.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_history(self):
        store = ResultStore(self.path)
        info = make_info(path=Path(self.tmp.name) / "a.mp4")
        store.add("ok", info, crf=27, saving=0.5)
        self.assertEqual(store.last(info.path)["action"], "ok")
        self.assertEqual(store.last(info.path)["crf"], 27)

    def test_latest_record_wins(self):
        store = ResultStore(self.path)
        info = make_info(path=Path(self.tmp.name) / "a.mp4")
        store.add("ok", info)
        store.add("skipped", info, reason="tagged")
        self.assertEqual(store.last(info.path)["action"], "skipped")
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_corrupt_lines_tolerated(self):
        self.path.write_text("not json\n", encoding="utf-8")
        store = ResultStore(self.path)
        info = make_info(path=Path(self.tmp.name) / "b.mp4")
        store.add("dryrun", info)
        self.assertIsNotNone(store.last(info.path))


class GatherFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "a.mp4").write_bytes(b"x")
        (root / "e.tmp.mkv").write_bytes(b"x")
        (root / "note.txt").write_bytes(b"x")
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "sub" / "c.mkv").write_bytes(b"x")
        (root / "sub" / "deep" / "d.mp4").write_bytes(b"x")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, files):
        return {f.name for f in files}

    def test_recursive_finds_and_filters(self):
        files = gather_files([self.root], True, {".mp4", ".mkv"})
        self.assertEqual(self.names(files), {"a.mp4", "c.mkv", "d.mp4"})

    def test_no_recursive_only_top_level(self):
        files = gather_files([self.root], False, {".mp4", ".mkv"})
        self.assertEqual(self.names(files), {"a.mp4"})

    def test_deduplicates_overlapping_paths(self):
        dup = gather_files([self.root, self.root], True, {".mp4", ".mkv"})
        self.assertEqual(len(dup), 3)

    def test_explicit_file_accepted(self):
        f = self.root / "a.mp4"
        files = gather_files([f, f], False, {".mp4"})
        self.assertEqual(len(files), 1)


class CleanStaleTmpTests(unittest.TestCase):
    def test_removes_only_matching_tmp_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            keep = root / "a.mp4"
            keep.write_bytes(b"x")
            stale = root / "b.tmp.mkv"
            stale.write_bytes(b"x")
            other = root / "c.tmp.txt"
            other.write_bytes(b"x")
            removed = clean_stale_tmp([keep], ".tmp.mkv")
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())
            self.assertTrue(keep.exists())


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
        final = resolve_final_path(src, target, ".mkv", "av1", keep=False)
        self.assertEqual(final, target)

    def test_in_place_replacement_allowed(self):
        src = self.touch("a.mkv")
        final = resolve_final_path(src, src, ".mkv", "av1", keep=False)
        self.assertEqual(final, src)

    def test_unrelated_target_is_not_overwritten(self):
        src = self.touch("a.mp4")
        victim = self.touch("a.mkv")
        final = resolve_final_path(src, victim, ".mkv", "av1", keep=False)
        self.assertEqual(final, self.root / "a.av1.mkv")
        self.assertTrue(victim.exists())

    def test_keep_mode_never_targets_source(self):
        src = self.touch("a.mkv")
        final = resolve_final_path(src, src, ".mkv", "av1", keep=True)
        self.assertEqual(final, self.root / "a.av1.mkv")

    def test_fallback_taken_raises(self):
        src = self.touch("a.mp4")
        self.touch("a.mkv")
        self.touch("a.av1.mkv")
        with self.assertRaises(RuntimeError) as cm:
            resolve_final_path(src, self.root / "a.mkv", ".mkv", "av1",
                               keep=False)
        self.assertIn("already exists", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

