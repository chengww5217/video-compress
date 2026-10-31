"""处理记录（results.jsonl）：断点续跑与统计。"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .logfmt import em
from .util import fmt_size_change

log = logging.getLogger("vcompress")

CACHED_ACTIONS = ("ok", "skipped")

# records 里的 action 是给机器看的，日志里要给人看
ACTION_LABELS = {
    "ok": "已压缩",
    "skipped": "已跳过",
    "failed": "失败",
    "dryrun": "预览",
    "aborted": "中止",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def is_permanent_failure(rec: dict) -> bool:
    """这条失败记录重跑也是同样结果吗。

    失败默认要重试：磁盘满、被 OOM 杀掉、临时目录写不下去，换个时间就好了。
    只有结论由片源和配置决定的失败（产物压不小、收益不够）才带 permanent，
    那种重试只是把同一个答案再算几个小时。
    """
    return rec.get("action") == "failed" and bool(rec.get("permanent"))


# 旧版 --remux 先扫关键帧体检，体检结论是「不用修 / 修不了」就记一条跳过。
# 现在 --remux 不再体检，这些结论作废了；不认出来的话，体检时被放过的文件
# 会被缓存永远挡在外面。原因串是旧代码写死的，往后不会再产生，只认这几种
# 原话，别的跳过结论（画质/体积闸门）照旧缓存。
STALE_REMUX_SKIP = re.compile(
    r"^(关键帧间隔与容器都没有问题"
    r"|容器 \S+ 没有可靠索引"       # 「目标容器也是它」「装不进」两种
    r"|扫关键帧失败"
    r"|读不到任何关键帧时间戳)"
)


def is_stale_remux_skip(rec: dict) -> bool:
    """这条记录是不是旧版 --remux 体检留下的跳过结论。"""
    return (rec.get("mode") == "remux" and rec.get("action") == "skipped"
            and bool(STALE_REMUX_SKIP.match(rec.get("reason") or "")))


def cached_reason(rec: dict) -> str:
    """[缓存] 日志里「为什么这次不处理它」那半句。

    失败必须把原因带上：光说"上次失败了，本次跳过"，人只会觉得这是个 bug。
    """
    if rec.get("action") == "failed":
        return (f"上次失败在「{rec.get('error') or '未记原因'}」，"
                f"片源和配置没变，重跑还是同样结论")
    if rec.get("mode") == "remux":
        label = "已重封装" if rec.get("action") == "ok" else "重封装时已跳过"
        return f"上次的结果是{label}"
    return f"上次的结果是{action_label(rec.get('action', ''))}"


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
            log.warning(f"压实 {self.path.name} 失败：{e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        log.debug(f"压实 {self.path.name}：{before} 行 -> "
                  f"{len(self.history)} 条记录")

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

    def is_cached(self, path: Path, mode: str = "compress"):
        """上次已经处理过、并且文件确实没变过时返回那条记录，否则 None。

        只按路径判断是不够的：同名文件被换成另一个片源（下载了更好的版本）时
        旧记录会让它被永远跳过。记录里已经有体积，顺手比一下就能发现。

        失败的记录一般不算「处理过」——那多半是环境问题，下次该重试；只有
        is_permanent_failure() 认定重跑无用的那种才算（见那个函数）。

        记录还区分模式：上次压缩过的文件，重封装模式照样要查（修 GOP）；
        上次重封装过的，压缩模式靠元数据标记跳过。模式对不上就不算缓存命中，
        老记录（没有 mode 字段）视为压缩模式。
        """
        rec = self.last(path)
        if not rec:
            return None
        if rec.get("action") not in CACHED_ACTIONS \
                and not is_permanent_failure(rec):
            return None
        if rec.get("mode", "compress") != mode:
            return None
        if is_stale_remux_skip(rec):
            log.info(f"[重查] {em(path.name)}：上次是旧版 --remux 体检后跳过的"
                     f"（{rec.get('reason')}），现在 --remux 不再体检，重新处理")
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
        log.info(f"[重查] {em(path.name)}：体积和上次记录不一致"
                 f"（{fmt_size_change(sorted(expected)[0], actual)}），重新处理")
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
