"""临时工作目录：无损样本片段放在哪里。

单独成模块、而且是进程级的一个设置，理由是这个决定横跨自适应搜索
（adaptive）、VMAF 抽检（verify）和打分（vmaf）三处，却和它们各自的职责
无关；穿成参数要在四五个函数签名里传一路，穿到最里面还是同一个值。

为什么必须能配：默认的系统临时目录在很多发行版上是 tmpfs，也就是内存盘。
无损样本一点都不小——实测 4K 片源的无损抽样约 500 Mbps，一段 20 秒就是
1.2 GB，抽检要参考段和待测段两份，自适应搜索还要同时留着几段参考。7.4 GB
的 /tmp 被两个并发 worker 撑爆之后，抽样失败会被当成「这次量不了 VMAF」
放过去——画质门槛就这么被跳过了。
"""

import tempfile
from pathlib import Path

_dir = None


def set_dir(path) -> "Path | None":
    """设定临时工作目录；None 或空表示用系统默认（tempfile 的行为）。

    目录建不出来不算致命错误：退回系统临时目录，至少还能干活。
    """
    global _dir
    if not path:
        _dir = None
        return None
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        _dir = None
        return None
    _dir = path
    return path


def get_dir() -> "Path | None":
    return _dir


def temp_dir(prefix: str):
    """一个会自动清理的临时目录，位置听 set_dir() 的。

    每次都确认一遍目录还在：一次运行可能跑好几十小时，中间被清理工具扫掉、
    或者盘掉线都有可能。目录不可用时退回系统临时目录——总比整个文件失败好。
    """
    if _dir is not None:
        try:
            _dir.mkdir(parents=True, exist_ok=True)
            return tempfile.TemporaryDirectory(prefix=prefix, dir=str(_dir))
        except OSError:
            pass
    return tempfile.TemporaryDirectory(prefix=prefix)
