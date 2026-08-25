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


class CompactionTests(unittest.TestCase):
    """这个文件是断点续跑的唯一依据：可以变小，但不能变错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "results.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write_lines(self, records):
        with open(self.path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def lines(self):
        return self.path.read_text(encoding="utf-8").strip().splitlines()

    def test_repeated_runs_of_the_same_files_are_compacted(self):
        paths = [str(self.root / f"{i}.mp4") for i in range(3)]
        self.write_lines([{"path": p, "action": "ok", "size": 100,
                           "new_size": 40, "run": n}
                          for n in range(400) for p in paths])
        store = ResultStore(self.path)
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual(len(store.history), 3)
        # 留下的必须是最后一次的记录
        for p in paths:
            self.assertEqual(store.history[p]["run"], 399)
        self.assertEqual(store.total_saved(), 3 * 60)

    def test_reload_after_compaction_sees_the_same_history(self):
        paths = [str(self.root / f"{i}.mp4") for i in range(2)]
        self.write_lines([{"path": p, "action": "skipped", "size": 10,
                           "reason": f"run {n}"}
                          for n in range(600) for p in paths])
        first = ResultStore(self.path).history
        second = ResultStore(self.path).history
        self.assertEqual(first, second)

    def test_a_long_but_healthy_ledger_is_left_alone(self):
        """一万个不同的文件就该有一万行，那不是垃圾。"""
        self.write_lines([{"path": str(self.root / f"{i}.mp4"),
                           "action": "ok", "size": 10, "new_size": 5}
                          for i in range(1200)])
        ResultStore(self.path)
        self.assertEqual(len(self.lines()), 1200)

    def test_a_short_ledger_is_left_alone(self):
        paths = [str(self.root / "a.mp4")] * 20
        self.write_lines([{"path": p, "action": "ok"} for p in paths])
        ResultStore(self.path)
        self.assertEqual(len(self.lines()), 20,
                         "rewriting a 20-line file buys nothing")

    def test_no_temp_file_is_left_behind(self):
        paths = [str(self.root / f"{i}.mp4") for i in range(2)]
        self.write_lines([{"path": p, "action": "ok"}
                          for _n in range(600) for p in paths])
        ResultStore(self.path)
        self.assertEqual([p.name for p in self.root.iterdir()],
                         ["results.jsonl"])

    def test_appending_after_compaction_still_works(self):
        src = self.root / "a.mp4"
        src.write_bytes(b"x" * 50)
        self.write_lines([{"path": str(src.resolve()), "action": "ok",
                           "size": 50, "run": n} for n in range(1100)])
        store = ResultStore(self.path)
        store.add("skipped", make_info(path=src, size=50), reason="tagged")
        self.assertEqual(len(self.lines()), 2)
        self.assertIsNotNone(store.is_cached(src))


if __name__ == "__main__":
    unittest.main()
