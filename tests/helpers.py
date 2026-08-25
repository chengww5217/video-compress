import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vcompress import config
from vcompress.probe import VideoInfo


def make_cfg():
    """测试用配置：直接取内置默认值，再改掉少数为了断言好写而收窄的项。

    这样 fixture 不可能和 DEFAULTS 结构漂移——以前两边各写一份，加一个键就
    要记得改两处。
    """
    cfg = config.defaults()
    cfg["skip"]["extensions"] = [".mp4", ".mkv"]
    # extra 用短串，参数拼装的断言才好读
    cfg["encode"]["av1"]["extra"] = "-svtav1-params tune=0"
    cfg["encode"]["x265"]["extra"] = "-x265-params aq-mode=3"
    cfg["run"]["results_file"] = "results.jsonl"
    return cfg


def make_info(**kw):
    base = dict(
        path=Path("movie.mp4"),
        container=".mp4",
        video_codec="h264",
        codec_family="h264",
        width=1920,
        height=1080,
        fps=30.0,
        bitrate=6_000_000,
        container_bitrate=0,
        duration=600.0,
        size=500_000_000,
        pix_fmt="yuv420p",
        comment="",
        color_primaries="bt709",
        color_transfer="bt709",
        color_space="bt709",
        color_range="tv",
        hdr_metadata=[],
        audio_codecs=["aac"],
        audio_bitrates=[192_000],
        audio_channels=[2],
        subtitle_codecs=[],
    )
    base.update(kw)
    return VideoInfo(**base)


class FakeTools:
    ffmpeg = "ffmpeg"
    ffprobe = "ffprobe"
