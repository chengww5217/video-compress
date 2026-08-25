"""处理记录（results.jsonl）：断点续跑与统计。"""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("vcompress")

CACHED_ACTIONS = ("ok", "skipped")


class ResultStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.history = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        self.history[rec.get("path", "")] = rec

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
