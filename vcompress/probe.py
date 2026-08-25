"""ffprobe 探测与「这个文件要不要压」的判定。"""

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import proc
from .util import to_float, to_int

CODEC_FAMILY = {
    "h264": "h264",
    "avc1": "h264",
    "hevc": "hevc",
    "hev1": "hevc",
    "hvc1": "hevc",
    "h265": "hevc",
    "av01": "av1",
    "av1": "av1",
    "vp9": "vp9",
    "vp09": "vp9",
    "mpeg2video": "mpeg2video",
    "mpeg4": "mpeg4",
    "flv1": "flv1",
    "wmv3": "wmv3",
}

if sys.platform == "win32":
    _FALLBACK_DIRS = (r"C:\Program Files\ffmpeg\bin", r"C:\Program Files\ffmpeg")
else:
    _FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


def _find_tool(config_value: str, name: str) -> str:
    def resolve(v):
        p = Path(v).expanduser()
        if p.is_dir():
            for cand in (p / f"{name}.exe", p / name):
                if cand.exists():
                    return str(cand)
            return None
        if p.exists():
            return str(p)
        return None

    if config_value:
        r = resolve(config_value)
        if r:
            return r
        raise FileNotFoundError(
            f"config.toml [tools] points at {config_value!r}, but no {name} "
            f"was found there"
        )
    w = shutil.which(name)
    if w:
        return w
    for d in _FALLBACK_DIRS:
        r = resolve(d)
        if r:
            return r
    raise FileNotFoundError(
        f"{name} not found. Install ffmpeg or set path in config.toml [tools]"
    )


class Tools:
    def __init__(self, cfg: dict):
        self.ffmpeg = _find_tool(cfg.get("ffmpeg", ""), "ffmpeg")
        self.ffprobe = _find_tool(cfg.get("ffprobe", ""), "ffprobe")


@dataclass
class VideoInfo:
    path: Path
    container: str
    video_codec: str = ""
    codec_family: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    bitrate: int = 0
    container_bitrate: int = 0
    duration: float = 0.0
    size: int = 0
    pix_fmt: str = ""
    comment: str = ""
    audio_codecs: list = field(default_factory=list)
    audio_bitrates: list = field(default_factory=list)
    audio_channels: list = field(default_factory=list)
    subtitle_codecs: list = field(default_factory=list)

    def video_bitrate(self) -> int:
        """视频轨码率；容器没给的话用总码率扣掉音轨估算。"""
        if self.bitrate > 0:
            return self.bitrate
        if self.container_bitrate > 0:
            audio = sum(b for b in self.audio_bitrates if b)
            rest = self.container_bitrate - audio
            return rest if rest > 0 else self.container_bitrate
        return 0


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den = float(den)
        if den == 0:
            return 0.0
        return float(num) / den
    except (ValueError, AttributeError):
        return 0.0


def _stream_bitrate(stream: dict) -> int:
    """matroska 通常不给 bit_rate，退回它写的 BPS 统计标签。"""
    tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
    bps = next((v for k, v in tags.items() if k == "bps" or k.startswith("bps-")),
               None)
    return to_int(stream.get("bit_rate")) or to_int(bps)


def probe(tools: Tools, path: Path) -> VideoInfo:
    cmd = [
        tools.ffprobe,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    # 走 run_managed：Ctrl+C 时这个 ffprobe 也要被杀掉，别留孤儿进程
    rc, out, err = proc.run_managed(cmd)
    if rc != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}: {err.strip()[:300]}")
    try:
        data = json.loads(out)
    except ValueError as e:
        raise RuntimeError(f"ffprobe returned unparsable JSON for "
                           f"{path.name}: {e}") from e
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astreams = [s for s in streams if s.get("codec_type") == "audio"]
    sstreams = [s for s in streams if s.get("codec_type") == "subtitle"]

    info = VideoInfo(path=path, container=path.suffix.lower())
    info.video_codec = vstream.get("codec_name", "")
    info.codec_family = CODEC_FAMILY.get(info.video_codec, "")
    info.width = to_int(vstream.get("width"))
    info.height = to_int(vstream.get("height"))
    info.fps = _parse_fps(vstream.get("r_frame_rate")) or \
        _parse_fps(vstream.get("avg_frame_rate"))
    info.pix_fmt = vstream.get("pix_fmt", "")
    info.bitrate = _stream_bitrate(vstream)
    info.container_bitrate = to_int(fmt.get("bit_rate"))
    info.duration = to_float(fmt.get("duration")) or \
        to_float(vstream.get("duration"))
    info.size = to_int(fmt.get("size"), default=0) or _file_size(path)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info.comment = str(tags.get("comment") or tags.get("description") or "")
    info.audio_codecs = [s.get("codec_name", "") for s in astreams]
    info.audio_bitrates = [_stream_bitrate(s) for s in astreams]
    info.audio_channels = [to_int(s.get("channels"), 2) for s in astreams]
    info.subtitle_codecs = [s.get("codec_name", "") for s in sstreams]
    return info


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def should_skip(info: VideoInfo, cfg: dict):
    marker = str(cfg["skip"].get("tag_marker", "vcompress"))
    if marker and marker.lower() in (info.comment or "").lower():
        return True, f"already processed by {marker}"

    min_size = int(cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return True, f"file too small ({info.size // 1024 // 1024} MB)"

    bpp_cfg = cfg["skip"]["bpp"]
    if info.codec_family and info.codec_family in bpp_cfg:
        fps = info.fps if info.fps > 0 else 25.0
        pixels = info.width * info.height
        vbr = info.video_bitrate()
        if vbr > 0 and pixels > 0:
            bpp = vbr / (pixels * fps)
            thr = float(bpp_cfg[info.codec_family])
            if bpp < thr:
                return True, (
                    f"{info.codec_family} at {bpp:.4f} bits/pixel "
                    f"is already below threshold {thr}"
                )

    ratio_cfg = cfg["skip"]["expected_ratio"]
    min_saving = float(cfg["skip"].get("min_saving_ratio", 0.15))
    if info.codec_family and info.codec_family in ratio_cfg:
        predicted = float(ratio_cfg[info.codec_family])
        saving = 1.0 - predicted
        if saving < min_saving:
            return True, (
                f"{info.codec_family} predicted saving {saving:.0%} "
                f"below required {min_saving:.0%}"
            )
    return False, ""
