import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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


def _find_tool(config_value: str, name: str) -> str:
    def resolve(v):
        p = Path(v)
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
    w = shutil.which(name)
    if w:
        return w
    for d in (r"C:\Program Files\ffmpeg\bin", r"C:\Program Files\ffmpeg"):
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
    duration: float = 0.0
    size: int = 0
    pix_fmt: str = ""
    tagged: bool = False
    audio_codecs: list = field(default_factory=list)
    audio_bitrates: list = field(default_factory=list)


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _parse_fps(rate: str) -> float:
    try:
        num, den = rate.split("/")
        den = float(den)
        if den == 0:
            return 0.0
        return float(num) / den
    except (ValueError, AttributeError):
        return 0.0


def probe(tools: Tools, path: Path) -> VideoInfo:
    cmd = [
        tools.ffprobe,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path.name}: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astreams = [s for s in streams if s.get("codec_type") == "audio"]

    info = VideoInfo(path=path, container=path.suffix.lower())
    info.video_codec = vstream.get("codec_name", "")
    info.codec_family = CODEC_FAMILY.get(info.video_codec, "")
    info.width = _to_int(vstream.get("width"))
    info.height = _to_int(vstream.get("height"))
    info.fps = _parse_fps(vstream.get("r_frame_rate")) or _parse_fps(vstream.get("avg_frame_rate"))
    info.pix_fmt = vstream.get("pix_fmt", "")
    info.bitrate = _to_int(vstream.get("bit_rate")) or _to_int(fmt.get("bit_rate"))
    info.duration = float(fmt.get("duration") or vstream.get("duration") or 0)
    info.size = _to_int(fmt.get("size"), default=path.stat().st_size)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    comment = str(tags.get("comment") or tags.get("description") or "")
    info.tagged = "vcompress" in comment.lower()
    info.audio_codecs = [s.get("codec_name", "") for s in astreams]
    info.audio_bitrates = [_to_int(s.get("bit_rate")) for s in astreams]
    return info


def should_skip(info: VideoInfo, cfg: dict):
    marker = cfg["skip"].get("tag_marker", "vcompress")
    if info.tagged:
        return True, f"already processed by {marker}"

    min_size = int(cfg["skip"].get("min_size_mb", 0)) * 1024 * 1024
    if info.size and info.size < min_size:
        return True, f"file too small ({info.size // 1024 // 1024} MB)"

    bpp_cfg = cfg["skip"]["bpp"]
    if info.codec_family and info.codec_family in bpp_cfg:
        fps = info.fps if info.fps > 0 else 25.0
        pixels = info.width * info.height
        if info.bitrate > 0 and pixels > 0:
            bpp = info.bitrate / (pixels * fps)
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
