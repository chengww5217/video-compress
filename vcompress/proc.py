"""所有 ffmpeg/ffprobe 子进程的注册表与取消开关。

单独成模块的理由：probe、encode、verify 都要起子进程，都要能被 Ctrl+C 立刻
杀干净。放在 encode.py 里会让 probe.py 反向依赖 encode。
"""

import subprocess
import threading

_active_procs = set()
_lock = threading.Lock()
_cancelled = threading.Event()


class ProcessError(RuntimeError):
    pass


def register(proc) -> None:
    with _lock:
        _active_procs.add(proc)


def unregister(proc) -> None:
    with _lock:
        _active_procs.discard(proc)


def terminate_all() -> None:
    """立即杀掉所有在跑的子进程，并置上取消标志。"""
    _cancelled.set()
    with _lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def is_cancelled() -> bool:
    return _cancelled.is_set()


def clear_cancel() -> None:
    _cancelled.clear()


def active_count() -> int:
    with _lock:
        return len(_active_procs)


def spawn(cmd: list, cwd=None, stdout=subprocess.PIPE,
          stderr=subprocess.PIPE) -> subprocess.Popen:
    """起一个已注册的子进程；调用方负责 unregister。"""
    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    register(proc)
    return proc


def run_managed(cmd: list, cwd=None) -> tuple:
    """跑一个短命子进程，注册后可被 terminate_all 立刻杀掉。

    返回 (returncode, stdout, stderr)。中断时先 kill 再收尸，不留僵尸进程。
    """
    proc = spawn(cmd, cwd=cwd)
    try:
        out, err = proc.communicate()
    except BaseException:
        try:
            proc.kill()
        except OSError:
            pass
        proc.communicate()
        raise
    finally:
        unregister(proc)
    return proc.returncode, out, err
