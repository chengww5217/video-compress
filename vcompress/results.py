"""处理记录（results.jsonl）：断点续跑与统计。"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("vcompress")

CACHED_ACTIONS = ("ok", "skipped")

# 只有攒够这么多行、且其中一半以上是被后来记录顶掉的旧行，才值得压实。
# 一次处理一万个文件是正常的（那一万行都还有效），反复 --force 重跑才会
# 让同一个路径攒下几十行。
COMPACT_MIN_LINES = 1000


class ResultStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.history = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        """读入历史，每个路径只留最后一条；旧行太多时顺手压实一次。"""
        total = 0
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                key = rec.get("path", "")
                # 先删再插：让这条记录排到末尾，压实后的顺序仍是时间顺序
                self.history.pop(key, None)
                self.history[key] = rec
        if total >= COMPACT_MIN_LINES and total > 2 * len(self.history):
            self._compact(total)

    def _compact(self, before: int) -> None:
        """把每个路径只剩一条的历史重写回去，先写临时文件再原子替换。

        这个文件是断点续跑的唯一依据，所以宁可压实失败也不能写坏：临时文件
        写完整了才 os.replace，中途挂掉时磁盘上仍是原来那份。
        """
        tmp = self.path.with_name(self.path.name + ".compact.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in self.history.values():
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning(f"cannot compact {self.path.name}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        log.info(f"compacted {self.path.name}: {before} -> "
                 f"{len(self.history)} record(s)")

    def last(self, path: Path):
        return self.history.get(str(path.resolve()))

    def add(self, action: str, info=None, **extra):
        rec = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "action": action,
            "path": str(info.path.resolve()) if info else None,
        }
        if info is not None:
            rec.update({
                "codec": info.video_codec,
                "size": info.size,
                "duration": round(info.duration, 3),
            })
        rec.update(extra)
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if rec["path"]:
                self.history[rec["path"]] = rec

    def is_cached(self, path: Path):
        """上次已经处理过、并且文件确实没变过时返回那条记录，否则 None。

        只按路径判断是不够的：同名文件被换成另一个片源（下载了更好的版本）时
        旧记录会让它被永远跳过。记录里已经有体积，顺手比一下就能发现。
        """
        rec = self.last(path)
        if not rec or rec.get("action") not in CACHED_ACTIONS:
            return None
        expected = self._expected_sizes(rec, path)
        if not expected:
            return rec  # 老记录没存体积，只能按路径信任
        try:
            actual = path.stat().st_size
        except OSError:
            return rec
        if actual in expected:
            return rec
        log.info(f"[recheck] {path.name}: size changed since the last run "
                 f"({sorted(expected)[0]} -> {actual} bytes); processing again")
        return None

    @staticmethod
    def _expected_sizes(rec: dict, path: Path) -> set:
        """这条记录成立时，磁盘上这个路径应该是多大。"""
        sizes = set()
        original = rec.get("size")
        produced = rec.get("new_size")
        output = rec.get("output")
        if rec.get("action") == "ok" and produced and output:
            try:
                if Path(output).resolve() == path.resolve():
                    return {int(produced)}  # 原地替换：现在躺在这里的是产物
            except OSError:
                pass
        if original:
            sizes.add(int(original))
        return sizes

    def total_saved(self) -> int:
        """历史累计省下的字节数。"""
        total = 0
        for rec in self.history.values():
            if rec.get("action") == "ok" and rec.get("size") \
                    and rec.get("new_size"):
                total += max(0, int(rec["size"]) - int(rec["new_size"]))
        return total
