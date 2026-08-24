import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json
import signal
import subprocess
import sys
import tempfile
import time
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
min_saving_ratio = 0.10
extensions = [".mp4", ".mkv"]
tag_marker = "vcompress"

[skip.bpp]
h264 = 0.05

[skip.expected_ratio]
h264 = 0.55

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
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(cls.root / "src.mp4"),
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
             "-pix_fmt", "yuv420p",
             str(cls.root / "keep_src.mp4")],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(cls.root / "src.mp4"), "-c", "copy",
             str(cls.root / "same_ext.mkv")],
            check=True, capture_output=True, timeout=120,
        )
        # collide.mp4 的输出名会撞上一个内容完全不同的 collide.mkv
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
             "-pix_fmt", "yuv420p",
             str(cls.root / "collide.mp4")],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=5:size=320x180:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-metadata", "title=UNRELATED_USER_FILE",
             str(cls.root / "collide.mkv")],
            check=True, capture_output=True, timeout=120,
        )

        # tricky.mp4 同时带 tmcd 数据流和 mov_text 字幕，两者都装不进 mkv
        (cls.root / "sub.srt").write_text(
            "1\n00:00:00,500 --> 00:00:01,500\nhello world\n\n",
            encoding="utf-8",
        )
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=15",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-i", str(cls.root / "sub.srt"),
             "-map", "0:v", "-map", "1:a", "-map", "2:s",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text",
             "-timecode", "00:00:00:00",
             str(cls.root / "tricky.mp4")],
            check=True, capture_output=True, timeout=120,
        )

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_vcompress(self, *extra, videos=("src.mp4", "garbage.mp4")):
        return subprocess.run(
            [sys.executable, "-m", "vcompress",
             *[str(self.root / v) for v in videos],
             "--config", str(self.root / "config.toml"), *extra],
            cwd=str(helpers.ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )

    def test_a_compress_replaces_and_keeps_bad_file(self):
        r = self.run_vcompress("--no-adaptive")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[ok] src.mp4", r.stdout)
        self.assertIn("[encode] src.mp4", r.stdout)
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
        r = self.run_vcompress("--force", videos=("src.mkv", "garbage.mp4"))
        self.assertIn("[skip] src.mkv", r.stdout)
        self.assertIn("already processed", r.stdout)

    def test_c_keep_flag_preserves_originals(self):
        r = self.run_vcompress("--no-adaptive", "--keep",
                               videos=("keep_src.mp4",))
        self.assertEqual(r.returncode, 0)
        self.assertIn("[ok] keep_src.mp4", r.stdout)
        self.assertIn("original kept", r.stdout)
        self.assertTrue((self.root / "keep_src.mp4").exists(),
                        "original must survive in keep mode")
        self.assertTrue((self.root / "keep_src.mkv").exists())

        records = [
            json.loads(line)
            for line in (self.root / "results.jsonl")
            .read_text(encoding="utf-8").strip().splitlines()
        ]
        rec = next(x for x in records
                   if x["action"] == "ok" and "keep_src" in x["path"])
        self.assertTrue(rec["kept"])

    def test_d_keep_same_extension_names_by_encoder(self):
        original = self.root / "same_ext.mkv"
        before = original.stat().st_size
        r = self.run_vcompress("--no-adaptive", "--keep",
                               videos=("same_ext.mkv",))
        self.assertEqual(r.returncode, 0)
        renamed = self.root / "same_ext.av1.mkv"
        self.assertTrue(renamed.exists())
        self.assertFalse((self.root / "same_ext.compressed.mkv").exists())
        IntegrationTests.prev_output_size = renamed.stat().st_size
        self.assertTrue(original.exists())
        self.assertEqual(original.stat().st_size, before,
                         "original must not be overwritten by same-name output")

    def test_e_keep_collision_fails_instead_of_overwriting(self):
        r = self.run_vcompress("--no-adaptive", "--keep", "--force",
                               videos=("same_ext.mkv",))
        self.assertEqual(r.returncode, 1)
        self.assertIn("[fail] same_ext.mkv", r.stdout)
        self.assertIn("already exists", r.stdout)
        prev = self.root / "same_ext.av1.mkv"
        self.assertTrue(prev.exists(), "previous output must not be touched")
        self.assertEqual(prev.stat().st_size, IntegrationTests.prev_output_size,
                         "previous output content must be unchanged")
        self.assertEqual(list(self.root.glob("*.tmp.mkv")), [])


    def test_f_unrelated_target_name_is_never_overwritten(self):
        victim = self.root / "collide.mkv"
        before_size = victim.stat().st_size
        before_title = self.ffprobe_field(victim, "format_tags=title")
        self.assertEqual(before_title, "UNRELATED_USER_FILE")

        r = self.run_vcompress("--no-adaptive", videos=("collide.mp4",))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[ok] collide.mp4", r.stdout)
        self.assertIn("writing to collide.av1.mkv instead", r.stdout)

        self.assertTrue(victim.exists(), "unrelated file must survive")
        self.assertEqual(victim.stat().st_size, before_size)
        self.assertEqual(self.ffprobe_field(victim, "format_tags=title"),
                         "UNRELATED_USER_FILE")
        self.assertTrue((self.root / "collide.av1.mkv").exists())
        self.assertFalse((self.root / "collide.mp4").exists(),
                         "original should still be replaced")

    def ffprobe_field(self, path, entries):
        fp = Tools({"ffmpeg": "", "ffprobe": ""}).ffprobe
        r = subprocess.run(
            [fp, "-v", "error", "-show_entries", entries, "-of", "csv=p=0",
             str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip()


    def test_h_data_stream_and_mov_text_survive_the_container_switch(self):
        r = self.run_vcompress("--no-adaptive", videos=("tricky.mp4",))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[ok] tricky.mp4", r.stdout)
        out = self.root / "tricky.mkv"
        self.assertTrue(out.exists())

        fp = Tools({"ffmpeg": "", "ffprobe": ""}).ffprobe
        pr = subprocess.run(
            [fp, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name", "-of", "json", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        streams = json.loads(pr.stdout)["streams"]
        kinds = [s["codec_type"] for s in streams]
        codecs = [s.get("codec_name") for s in streams]
        self.assertIn("av1", codecs)
        self.assertIn("aac", codecs)
        self.assertIn("subrip", codecs, "mov_text should be converted to srt")
        self.assertNotIn("data", kinds, "tmcd data stream must be dropped")
        self.assertEqual(kinds.count("subtitle"), 1)

    def test_i_custom_tag_marker_round_trips(self):
        cfg = self.root / "marker.toml"
        cfg.write_text(
            (self.root / "config.toml").read_text(encoding="utf-8")
            .replace('tag_marker = "vcompress"', 'tag_marker = "zzmark"')
            .replace(f"{self.root.as_posix()}/results.jsonl",
                     f"{self.root.as_posix()}/marker-results.jsonl"),
            encoding="utf-8",
        )
        ff = Tools({"ffmpeg": "", "ffprobe": ""}).ffmpeg
        subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=duration=2:size=160x90:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
             "-pix_fmt", "yuv420p", str(self.root / "marker.mp4")],
            check=True, capture_output=True, timeout=120,
        )

        def run(*videos):
            return subprocess.run(
                [sys.executable, "-m", "vcompress",
                 *[str(self.root / v) for v in videos],
                 "--config", str(cfg), "--no-adaptive"],
                cwd=str(helpers.ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=300,
            )

        first = run("marker.mp4")
        self.assertEqual(first.returncode, 0, first.stdout)
        out = self.root / "marker.mkv"
        self.assertTrue(out.exists())
        self.assertIn("zzmark",
                      self.ffprobe_field(out, "format_tags=COMMENT"))

        second = run("marker.mkv")
        self.assertIn("[skip] marker.mkv", second.stdout)
        self.assertIn("already processed by zzmark", second.stdout)

    def test_g_sigint_kills_all_workers_promptly(self):
        # 用慢 preset + 稍大画面，保证发信号时两个 worker 都还在编码
        slow_cfg = self.root / "slow.toml"
        slow_cfg.write_text(
            (self.root / "config.toml").read_text(encoding="utf-8")
            .replace('preset = "8"', 'preset = "2"')
            .replace(f"{self.root.as_posix()}/results.jsonl",
                     f"{self.root.as_posix()}/slow-results.jsonl"),
            encoding="utf-8",
        )
        ff = Tools({"ffmpeg": "", "ffprobe": ""}).ffmpeg
        names = ["slow1.mp4", "slow2.mp4"]
        for n in names:
            subprocess.run(
                [ff, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i",
                 "testsrc2=duration=20:size=640x360:rate=30",
                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12",
                 "-pix_fmt", "yuv420p", str(self.root / n)],
                check=True, capture_output=True, timeout=180,
            )

        proc = subprocess.Popen(
            [sys.executable, "-m", "vcompress",
             *[str(self.root / n) for n in names],
             "--config", str(slow_cfg), "--no-adaptive", "--workers", "2"],
            cwd=str(helpers.ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 60
        try:
            time.sleep(8)
            self.assertIsNone(proc.poll(), "encode finished before SIGINT")
            proc.send_signal(signal.SIGINT)
            try:
                out = proc.communicate(timeout=max(5, deadline - time.monotonic()))[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("SIGINT did not stop the run: workers were not killed")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 130, out)
        self.assertIn("interrupted", out)
        self.assertEqual(list(self.root.glob("slow*.tmp.mkv")), [],
                         "partial temp files must be removed on interrupt")
        for n in names:
            self.assertTrue((self.root / n).exists(),
                            "originals must survive an interrupt")


if __name__ == "__main__":
    unittest.main()

