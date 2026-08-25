import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import make_info
from vcompress.results import ResultStore


class ResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "results.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_history(self):
        store = ResultStore(self.path)
        info = make_info(path=self.root / "a.mp4")
        store.add("ok", info, crf=27, saving=0.5)
        self.assertEqual(store.last(info.path)["action"], "ok")
        self.assertEqual(store.last(info.path)["crf"], 27)

    def test_latest_record_wins(self):
        store = ResultStore(self.path)
        info = make_info(path=self.root / "a.mp4")
        store.add("ok", info)
        store.add("skipped", info, reason="tagged")
        self.assertEqual(store.last(info.path)["action"], "skipped")
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_corrupt_lines_tolerated(self):
        self.path.write_text("not json\n[1,2,3]\n", encoding="utf-8")
        store = ResultStore(self.path)
        info = make_info(path=self.root / "b.mp4")
        store.add("dryrun", info)
        self.assertIsNotNone(store.last(info.path))

    def test_total_saved_sums_successful_runs(self):
        store = ResultStore(self.path)
        for name, size, new in (("a.mp4", 100, 40), ("b.mp4", 200, 50)):
            store.add("ok", make_info(path=self.root / name, size=size),
                      new_size=new)
        store.add("failed", make_info(path=self.root / "c.mp4", size=999))
        self.assertEqual(store.total_saved(), 60 + 150)


class CacheFreshnessTests(unittest.TestCase):
    """只按路径判断缓存会让换过内容的同名文件被永远跳过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ResultStore(self.root / "results.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, size):
        p = self.root / name
        p.write_bytes(b"x" * size)
        return p

    def test_unseen_file_is_not_cached(self):
        self.assertIsNone(self.store.is_cached(self.write("a.mp4", 10)))

    def test_skipped_file_with_the_same_size_is_cached(self):
        src = self.write("a.mp4", 50)
        self.store.add("skipped", make_info(path=src, size=50), reason="tagged")
        self.assertIsNotNone(self.store.is_cached(src))

    def test_skipped_file_that_changed_size_is_reprocessed(self):
        src = self.write("a.mp4", 50)
        self.store.add("skipped", make_info(path=src, size=50), reason="tagged")
        src.write_bytes(b"y" * 90)
        self.assertIsNone(self.store.is_cached(src),
                          "a different file at the same path must be examined")

    def test_in_place_result_is_matched_against_the_output_size(self):
        """原地替换后，磁盘上躺着的是产物，不是原文件。"""
        src = self.write("a.mkv", 40)
        self.store.add("ok", make_info(path=src, size=100),
                       output=str(src), new_size=40, kept=False)
        self.assertIsNotNone(self.store.is_cached(src))

    def test_kept_original_is_matched_against_the_original_size(self):
        src = self.write("a.mp4", 100)
        self.store.add("ok", make_info(path=src, size=100),
                       output=str(self.root / "a.av1.mkv"), new_size=40,
                       kept=True)
        self.assertIsNotNone(self.store.is_cached(src))

    def test_failed_runs_are_never_cached(self):
        src = self.write("a.mp4", 50)
        self.store.add("failed", make_info(path=src, size=50), error="boom")
        self.assertIsNone(self.store.is_cached(src))

    def test_records_without_a_size_are_trusted(self):
        src = self.write("a.mp4", 50)
        self.store.add("skipped", None, path=str(src.resolve()),
                       reason="legacy record")
        self.assertIsNotNone(self.store.is_cached(src))

    def test_history_survives_a_reload(self):
        src = self.write("a.mp4", 50)
        self.store.add("skipped", make_info(path=src, size=50), reason="tagged")
        reloaded = ResultStore(self.store.path)
        self.assertIsNotNone(reloaded.is_cached(src))
        rec = json.loads(self.store.path.read_text(
            encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["action"], "skipped")


if __name__ == "__main__":
    unittest.main()
