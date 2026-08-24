import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vcompress.probe import VideoInfo


def make_cfg():
    return {
        "tools": {"ffmpeg": "", "ffprobe": ""},
        "skip": {
            "min_size_mb": 20,
            "extensions": [".mp4", ".mkv"],
            "tag_marker": "vcompress",
            "bpp": {"h264": 0.05, "hevc": 0.03, "vp9": 0.03, "av1": 0.02},
            "expected_ratio": {"h264": 0.55, "hevc": 0.78, "vp9": 0.80, "av1": 0.92},
            "min_saving_ratio": 0.15,
        },
        "encode": {
            "encoder": "av1",
            "output_container": ".mkv",
            "av1": {"codec": "libsvtav1", "preset": "2", "crf": 28,
                    "extra": "-svtav1-params tune=0"},
            "x265": {"codec": "libx265", "preset": "placebo", "crf": 20,
                     "extra": "-x265-params aq-mode=3"},
        },
        "adaptive": {
            "enabled": True,
            "target_vmaf": 95.0,
            "crf_min": 16,
            "crf_max": 44,
            "sample_seconds": 30,
            "search_step": 2,
        },
        "audio": {"transcode_over_kbps": 512, "opus_kbps": 160},
        "verify": {
            "decode_check": True,
            "max_duration_diff_s": 0.5,
            "min_actual_saving": 0.05,
            "vmaf_enabled": True,
            "vmaf_segments": 3,
            "vmaf_sample_seconds": 20,
            "vmaf_threshold": 93.0,
        },
        "run": {
            "workers": 1,
            "results_file": "results.jsonl",
            "log_file": "logs/vcompress.log",
            "use_recycle_bin": False,
        },
    }


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
        audio_codecs=["aac"],
        audio_bitrates=[192_000],
        audio_channels=[2],
        subtitle_codecs=[],
    )
    base.update(kw)
    return VideoInfo(**base)
