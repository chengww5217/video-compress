"""所有 ffmpeg/ffprobe 子进程的注册表与取消开关。

单独成模块的理由：probe、encode、verify 都要起子进程，都要能被 Ctrl+C 立刻
杀干净。放在 encode.py 里会让 probe.py 反向依赖 encode。
"""

import re
import subprocess
import threading

_active_procs = set()
_lock = threading.Lock()
_cancelled = threading.Event()


class Cancelled(KeyboardInterrupt):
    """已经取消了，不该再起新的子进程。

    继承 KeyboardInterrupt 而不是 Exception：一次 Ctrl+C 之后，任何还在
    半路上的 worker 都应该按「被中断」收尾，而不是被某个 `except Exception`
    当成"这个文件失败了"接住，然后接着干活。
    """


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


def spawn(cmd: list, cwd=None, stdout=subprocess.PIPE,
          stderr=subprocess.PIPE) -> subprocess.Popen:
    """起一个已注册的子进程；调用方负责 unregister。

    取消之后一律拒绝启动：terminate_all 只杀得掉「已经在跑的」，拦不住之后
    才起的。实际踩过的坑是 Ctrl+C 打断了自适应搜索的样本编码，搜索把它当成
    「搜索失败」回退到固定 CRF，紧接着起了一个全新的、要跑几小时的编码——
    汇总都已经打完了。
    """
    if _cancelled.is_set():
        raise Cancelled("已经中断了，不再启动新的 ffmpeg 进程")
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


# ----------------------------------------------------------------- 错误信息提取

# ffmpeg 的进度刷新和编码器的收尾统计，对「为什么失败」毫无信息量
_NOISE_RE = re.compile(
    r"^(frame|size)=|\bspeed=\s*[\d.]+x|\bkb/s:|^\s*\[?libx264.*\b(frame [IPB]:"
    r"|mb [IPB] |coded y,|i16 v,|i8 v,|i4 v,|i8c dc,|Weighted P-Frames|"
    r"ref [PB] L[01]|consecutive B-frames|final ratefactor|8x8 transform|"
    r"direct mvs|SSIM|PSNR|profile |using SAR|using cpu capabilities)"
)
# 真正说明原因的那几个词。ffmpeg 的错误行可能被几十行统计压在后面，取尾巴
# 会正好把它挤掉——磁盘写满的 "No space left on device" 就是这么丢的
_CAUSE_RE = re.compile(
    r"(?i)\b(error|failed|invalid|unsupported|no space|not permitted|"
    r"permission denied|not found|unable to|cannot |can't |denied|"
    r"out of memory|broken pipe|too many)"
)


def error_tail(err: str, limit: int = 300) -> str:
    """从 ffmpeg 的 stderr 里挑出「为什么失败」，压成一行。

    直接取 `err[-300:]` 有两个毛病：进度行和编码器的收尾统计会把真正的错误
    行挤出窗口（实际发生过——磁盘写满的提示被 x264 的 20 行统计顶掉，只剩一句
    没有信息量的 "Conversion failed!"）；多行文本塞进日志还会让一条记录跨行，
    按行解析的日志全乱。
    """
    lines = [ln.strip() for ln in (err or "").splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _NOISE_RE.search(ln)]
    causes = [ln for ln in lines if _CAUSE_RE.search(ln)]
    picked = (causes or lines)[-3:]
    return " / ".join(picked)[-limit:]
