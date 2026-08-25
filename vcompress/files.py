"""文件发现、命名与删除策略。

这个模块的铁律：**永不覆盖一个已经存在的文件**，也永不让两个待处理文件写到
同一个路径上。
"""

import logging
import os
import threading
from pathlib import Path

from .logfmt import em

log = logging.getLogger("vcompress")

# 本工具自己产出的中间/废弃产物，扫描时必须排除，否则下一轮会把它们当片源
INTERMEDIATE_INFIXES = (".tmp.", ".failed.")

TMP_INFIX = ".tmp"
FAILED_INFIX = ".failed"


def is_intermediate(name: str) -> bool:
    return any(infix in name for infix in INTERMEDIATE_INFIXES)


def tmp_path(src: Path, out_ext: str) -> Path:
    """临时文件名。

    带上源文件的**完整**名字（含扩展名）而不是 stem：同一个目录里的 a.mp4 和
    a.avi 用 stem 命名会得到同一个 a.tmp.mkv，并发时两个 ffmpeg 往同一个文件
    写，赢的那个还会被当成对方的产物校验通过——静默的数据损坏。
    """
    return src.with_name(src.name + TMP_INFIX + out_ext)


def failed_path(src: Path, out_ext: str) -> Path:
    return src.with_name(src.name + FAILED_INFIX + out_ext)


def gather_files(paths, recursive: bool, extensions: set):
    files = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            if p.suffix.lower() in extensions and not is_intermediate(p.name):
                files.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in it:
                if f.is_file() and f.suffix.lower() in extensions \
                        and not is_intermediate(f.name):
                    files.append(f)
    seen = set()
    unique = []
    for f in files:
        r = str(f.resolve())
        if r not in seen:
            seen.add(r)
            unique.append(f)
    return sorted(unique)


def clean_stale_tmp(files, out_ext: str):
    """只清理待处理文件自己留下的临时文件，不碰目录里同后缀的其它文件。"""
    removed = 0
    for f in files:
        t = tmp_path(f, out_ext)
        if not t.exists():
            continue
        try:
            t.unlink()
            removed += 1
            log.warning(f"清掉了上次留下的临时文件 {em(t.name)}")
        except OSError as e:
            log.error(f"删不掉残留的临时文件 {t}：{e}")
    return removed


def same_path(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return str(a.resolve()) == str(b.resolve())


def resolve_final_path(src: Path, target: Path, out_ext: str, encoder: str,
                       keep: bool, logger=None, taken=()) -> Path:
    """决定最终输出路径，任何情况下都不覆盖已存在的其它文件。

    非 keep 模式下 target 正好是原文件时属于预期内的原地替换；其余情况只要
    target 已被占用，就退让到 <名字>.<编码器><扩展名>，该名字也被占用则失败。

    `taken` 是「磁盘上还不存在、但已经被别的在处理文件预定了」的路径集合。
    并发时 a.mp4 和 a.avi 的目标都是 a.mkv，只看磁盘会两个都放行。
    """
    reserved = {str(p) for p in taken}

    def busy(p: Path) -> bool:
        return p.exists() or str(p) in reserved

    in_place = not keep and same_path(target, src)
    if not busy(target) or (in_place and str(target) not in reserved):
        return target
    alt = src.with_name(f"{src.stem}.{encoder}{out_ext}")
    if busy(alt):
        raise RuntimeError(
            f"输出名 {target.name} 已被占用，不覆盖已有文件"
            f"（备用名 {alt.name} 也被占了）"
        )
    if logger:
        logger.warning(f"{target.name} 已存在且不是源文件，改写到 "
                       f"{em(alt.name)}")
    return alt


class OutputRegistry:
    """并发时给输出路径做预留，避免两个源文件抢同一个目标名。

    只看磁盘是不够的：目标文件是编码结束时才落地的，两个 worker 在编码开始
    时都会觉得目标名空着。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = {}  # 输出路径 -> 持有者（源文件路径）

    def claim(self, src: Path, target: Path, out_ext: str, encoder: str,
              keep: bool, logger=None) -> Path:
        owner_key = str(src.resolve())
        with self._lock:
            taken = [p for p, owner in self._owner.items() if owner != owner_key]
            final = resolve_final_path(src, target, out_ext, encoder, keep,
                                       logger, taken)
            self._owner[str(final)] = owner_key
            return final

    def release(self, src: Path) -> None:
        owner_key = str(src.resolve())
        with self._lock:
            for path in [p for p, o in self._owner.items() if o == owner_key]:
                del self._owner[path]


def keep_failed_output(tmp: Path, src: Path, out_ext: str):
    """校验没过时按需留下产物，别让几小时的编码结果直接消失。

    命名 <源文件名>.failed<扩展名>，名字被占就不留（本项目一律不覆盖已有文件）。
    """
    if not tmp.exists():
        return None
    dest = failed_path(src, out_ext)
    if dest.exists():
        log.warning(f"{dest.name} 已存在，只能丢弃产物 {tmp.name}")
        return None
    try:
        os.replace(tmp, dest)
    except OSError as e:
        log.error(f"留不下校验失败的产物 {dest.name}：{e}")
        return None
    log.warning(f"校验没过的产物留成了 {em(dest.name)} 供检查"
                f"（原文件未动）")
    return dest


def remove_tmp(tmp: Path) -> None:
    if tmp.exists():
        try:
            tmp.unlink()
            log.debug(f"清掉临时文件 {tmp.name}")
        except OSError as e:
            log.warning(f"删不掉临时文件 {tmp.name}：{e}")


def delete_original(path: Path, recycle: bool) -> None:
    if recycle:
        trash(path)
    else:
        path.unlink()


def trash(path: Path) -> None:
    try:
        from send2trash import send2trash
    except ImportError:
        log.warning("没装 send2trash，改为直接永久删除")
        path.unlink()
        return
    send2trash(str(path))
