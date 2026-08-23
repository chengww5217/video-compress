import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from vcompress.probe import Tools

INTEGRATION_CONFIG = """
[tools]
ffmpeg = ""
ffprobe = ""

[skip]
min_size_mb = 0
extensions = [".mp4", ".mkv"]
tag_marker = "vcompress"

[skip.bpp]
h264 = 0.05

[skip.expected_ratio]
h264 = 0.55

min_saving_ratio = 0.10

[encode]
encoder = "av1"
output_container = ".mkv"

[encode.av1]
codec = "libsvtav1"
preset = "8"
crf = 30
extra = ""

[encode.x265]
codec = "libx265"
preset = "medium"
crf = 20
extra = ""

[adaptive]
enabled = false
target_vmaf = 95.0
crf_min = 16
crf_max = 44
sample_seconds = 5
search_step = 4

[audio]
transcode_over_kbps = 512
opus_kbps = 160

[verify]
decode_check = true
max_duration_diff_s = 0.5
min_actual_saving = 0.05
vmaf_enabled = false
vmaf_segments = 2
vmaf_sample_seconds = 5
vmaf_threshold = 90.0

[run]
workers = 1
results_file = "{root}/results.jsonl"
log_file = "{root}/logs/test.log"
use_recycle_bin = false
"""


def tools_available():
    try:
        Tools({"ffmpeg": "", "ffprobe": ""})
        return True
    except FileNotFoundError:
        return False


@unittest.skipUnless(tools_available(), "ffmpeg/ffprobe not found")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="vc_it_"))
        (cls.root / "config.toml").write_text(
            INTEGRATION_CONFIG.replace("{root}", cls.root.as_posix()),
            encoding="utf-8",
        )
        ff = Tools({"ffmpeg": "", "ffprobe": ""}).ffmpeg
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=15",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k",
             str(cls.root / "src.mp4")],
            check=True, capture_output=True, timeout=120,
        )
        (cls.root / "garbage.mp4").write_bytes(b"\x00" * 65536)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_vcompress(self, *extra, src_name="src.mp4"):
        return subprocess.run(
            [sys.executable, "-m", "vcompress",
             str(self.root / src_name), str(self.root / "garbage.mp4"),
             "--config", str(self.root / "config.toml"), *extra],
            cwd=str(helpers.ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )

    def test_a_compress_replaces_and_keeps_bad_file(self):
        r = self.run_vcompress("--no-adaptive")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[ok] src.mp4", r.stdout)
        self.assertIn("[fail] garbage.mp4", r.stdout)
        self.assertFalse((self.root / "src.mp4").exists(),
                         "original should be deleted after success")
        out = self.root / "src.mkv"
        self.assertTrue(out.exists())
        self.assertEqual(list(self.root.glob("*.tmp.mkv")), [],
                         "temp file should be cleaned up")

        records = [
            json.loads(line)
            for line in (self.root / "results.jsonl")
            .read_text(encoding="utf-8").strip().splitlines()
        ]
        actions = {rec["action"] for rec in records}
        self.assertIn("ok", actions)
        self.assertIn("failed", actions)
        ok_rec = next(rec for rec in records if rec["action"] == "ok")
        self.assertGreater(ok_rec["saving"], 0)

        fp = Tools({"ffmpeg": "", "ffprobe": ""}).ffprobe
        pr = subprocess.run(
            [fp, "-v", "error", "-show_entries",
             "format_tags=COMMENT:stream=codec_name", "-of", "json", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(pr.stdout)
        tags = {k.lower(): v for k, v in data["format"]["tags"].items()}
        self.assertIn("vcompress", tags.get("comment", ""))
        codecs = {s["codec_name"] for s in data["streams"]}
        self.assertEqual(codecs, {"av1", "aac"})

    def test_b_rerun_skips_via_tag_even_with_force(self):
        r = self.run_vcompress("--force", src_name="src.mkv")
        self.assertIn("[skip] src.mkv", r.stdout)
        self.assertIn("already processed", r.stdout)


if __name__ == "__main__":
    unittest.main()
