import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import make_cfg, make_info
from vcompress import adaptive, caps, encode, files, pipeline, proc, verify
from vcompress.pipeline import RunContext, RunStats
from vcompress.results import ResultStore


class PipelineTestCase(unittest.TestCase):
    """用替身挡掉 ffmpeg，只验证流程编排：临时文件、原文件、记录、返回值。"""

    def setUp(self):
        proc.clear_cancel()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.src = self.root / "a.mp4"
        self.src.write_bytes(b"o" * 1000)
        self.cfg = make_cfg()
        self.cfg["adaptive"]["enabled"] = False
        self.cfg["skip"]["min_size_mb"] = 0
        self.store = ResultStore(self.root / "results.jsonl")
        self.info = make_info(path=self.src, size=1000, duration=10.0)
        self.encoded = b"e" * 400

    def tearDown(self):
        proc.clear_cancel()
        self.tmpdir.cleanup()

    def ctx(self, **kw):
        return RunContext(
            cfg=self.cfg,
            tools=helpers.FakeTools(),
            caps=caps.Caps(encoders=frozenset({"libsvtav1", "libx264"}),
                             filters=frozenset({"libvmaf"})),
            store=self.store,
            outputs=files.OutputRegistry(),
            stats=RunStats(),
            encoder="av1",
            **kw,
        )

    def tmp_file(self):
        return files.tmp_path(self.src, ".mkv")

    def fake_encode(self, payload=None, boom=None):
        """替身：写出临时文件，然后按需抛异常。"""
        data = self.encoded if payload is None else payload

        def run_encode(cmd, duration, cb=None):
            Path(cmd[-1]).write_bytes(data)
            if cb:
                cb(50.0, 1.0, 2.0)
            if boom is not None:
                raise boom

        return run_encode

    def patches(self, run_encode, verify_ok=True, out_info=None):
        out = out_info or make_info(path=self.tmp_file(), video_codec="av1",
                                    size=len(self.encoded), duration=10.0)

        def probe_side_effect(tools, path):
            return self.info if Path(path) == self.src else out

        run_all = (mock.Mock(return_value=(95.0, 94.5)) if verify_ok
                   else mock.Mock(side_effect=verify.VerifyError("vmaf 40")))
        return (
            mock.patch.object(pipeline.probe, "probe",
                              side_effect=probe_side_effect),
            mock.patch.object(pipeline.encode, "run_encode", run_encode),
            mock.patch.object(pipeline.verify, "run_all", run_all),
        )

    def run_one(self, run_encode, verify_ok=True, **ctx_kw):
        ctx = self.ctx(**ctx_kw)
        p1, p2, p3 = self.patches(run_encode, verify_ok)
        with p1, p2, p3:
            return ctx, pipeline.process_file(ctx, self.src)


class HappyPathTests(PipelineTestCase):
    def test_output_replaces_the_original(self):
        ctx, outcome = self.run_one(self.fake_encode())
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual((self.root / "a.mkv").read_bytes(), self.encoded)
        self.assertFalse(self.src.exists(), "original must be gone")
        self.assertFalse(self.tmp_file().exists())
        self.assertEqual(ctx.stats.saved_bytes, 600)

    def test_keep_mode_leaves_the_original_alone(self):
        ctx, outcome = self.run_one(self.fake_encode(), keep=True)
        self.assertEqual(outcome, pipeline.OK)
        self.assertTrue(self.src.exists())
        self.assertTrue((self.root / "a.mkv").exists())
        rec = self.store.last(self.src)
        self.assertTrue(rec["kept"])

    def test_result_record_carries_the_numbers(self):
        _ctx, _ = self.run_one(self.fake_encode())
        rec = self.store.last(self.src)
        self.assertEqual(rec["action"], "ok")
        self.assertEqual(rec["new_size"], len(self.encoded))
        self.assertEqual(rec["size"], 1000)
        self.assertAlmostEqual(rec["saving"], 0.6, places=3)

    def test_existing_target_is_not_overwritten(self):
        victim = self.root / "a.mkv"
        victim.write_bytes(b"precious")
        _ctx, outcome = self.run_one(self.fake_encode())
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual(victim.read_bytes(), b"precious")
        self.assertEqual((self.root / "a.av1.mkv").read_bytes(), self.encoded)


class InterruptTests(PipelineTestCase):
    """Ctrl+C 不能留下半成品——串行模式下这条路径以前是漏的。"""

    def test_keyboard_interrupt_removes_the_temp_file_and_propagates(self):
        ctx = self.ctx(show_progress=False)
        p1, p2, p3 = self.patches(self.fake_encode(boom=KeyboardInterrupt()))
        with p1, p2, p3:
            with self.assertRaises(KeyboardInterrupt):
                pipeline.process_file(ctx, self.src)
        self.assertFalse(self.tmp_file().exists(),
                         "a serial Ctrl+C must clean up its own temp file")
        self.assertTrue(self.src.exists(), "original must survive")
        self.assertIsNone(self.store.last(self.src),
                          "an interrupt is not a failure worth recording")

    def test_cancelled_flag_reports_aborted_and_cleans_up(self):
        """并发模式：信号打在主线程，worker 看到的是子进程被杀后的报错。"""
        def run_encode(cmd, duration, cb=None):
            Path(cmd[-1]).write_bytes(b"partial")
            proc.terminate_all()
            raise encode.EncodeError("ffmpeg exited -9")

        ctx, outcome = self.run_one(run_encode)
        self.assertEqual(outcome, pipeline.ABORTED)
        self.assertFalse(self.tmp_file().exists())
        self.assertTrue(self.src.exists())
        self.assertIsNone(self.store.last(self.src))
        self.assertEqual(ctx.stats.counts["aborted"], 1)

    def test_interrupt_never_keeps_a_failed_output(self):
        self.cfg["verify"]["keep_failed_output"] = True
        ctx = self.ctx()
        p1, p2, p3 = self.patches(self.fake_encode(boom=KeyboardInterrupt()))
        with p1, p2, p3:
            with self.assertRaises(KeyboardInterrupt):
                pipeline.process_file(ctx, self.src)
        self.assertEqual(list(self.root.glob("*.failed.mkv")), [])


class FailureTests(PipelineTestCase):
    def test_verify_failure_discards_the_output(self):
        ctx, outcome = self.run_one(self.fake_encode(), verify_ok=False)
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertFalse(self.tmp_file().exists())
        self.assertTrue(self.src.exists())
        self.assertFalse((self.root / "a.mkv").exists())
        rec = self.store.last(self.src)
        self.assertEqual(rec["action"], "failed")
        self.assertIn("vmaf 40", rec["error"])

    def test_keep_failed_output_renames_instead_of_deleting(self):
        self.cfg["verify"]["keep_failed_output"] = True
        _ctx, outcome = self.run_one(self.fake_encode(), verify_ok=False)
        self.assertEqual(outcome, pipeline.FAILED)
        kept = self.root / "a.mp4.failed.mkv"
        self.assertTrue(kept.exists())
        self.assertEqual(kept.read_bytes(), self.encoded)
        self.assertTrue(self.src.exists())

    def test_encoder_failure_is_recorded(self):
        run_encode = self.fake_encode(boom=encode.EncodeError("bad params"))
        _ctx, outcome = self.run_one(run_encode)
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertIn("bad params", self.store.last(self.src)["error"])

    def test_probe_failure_is_recorded_without_touching_the_file(self):
        ctx = self.ctx()
        with mock.patch.object(pipeline.probe, "probe",
                               side_effect=RuntimeError("ffprobe failed")):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertTrue(self.src.exists())
        rec = self.store.last(self.src)
        self.assertEqual(rec["stage"], "probe")

    def test_system_exit_is_not_swallowed(self):
        ctx = self.ctx()
        p1, p2, p3 = self.patches(self.fake_encode(boom=SystemExit(3)))
        with p1, p2, p3:
            with self.assertRaises(SystemExit):
                pipeline.process_file(ctx, self.src)


class SkipAndDryRunTests(PipelineTestCase):
    def test_tagged_file_is_skipped(self):
        self.info.comment = "vcompress-v1"
        ctx = self.ctx()
        with mock.patch.object(pipeline.probe, "probe", return_value=self.info):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("之前压过了", self.store.last(self.src)["reason"])

    def test_dry_run_touches_nothing(self):
        ctx = self.ctx(dry_run=True)
        with mock.patch.object(pipeline.probe, "probe", return_value=self.info):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.DRYRUN)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["a.mp4", "results.jsonl"])


class PreEncodeRejectTests(PipelineTestCase):
    """搜索完就该知道值不值得压，而不是几小时后在校验里才发现。"""

    def setUp(self):
        super().setUp()
        self.cfg["adaptive"]["enabled"] = True
        # 1000 字节 / 10 秒 = 800 bps 的源；音轨算 0 便于对账
        self.info = make_info(path=self.src, size=1000, duration=10.0,
                              audio_codecs=[], audio_bitrates=[],
                              audio_channels=[])

    def run_with_choice(self, choice):
        ctx = self.ctx()
        p1, p2, p3 = self.patches(self.fake_encode())
        with p1, p2, p3, mock.patch.object(pipeline.adaptive,
                                           "pick_crf",
                                           return_value=choice):
            return ctx, pipeline.process_file(ctx, self.src)

    def test_predicted_saving_too_small_skips_before_encoding(self):
        # 700 bps * 10s / 8 = 875 字节，只省 12.5% < 15%
        ctx, outcome = self.run_with_choice(adaptive.CrfChoice(30, 700, True, 96.0))
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertTrue(self.src.exists(), "original must be untouched")
        self.assertFalse((self.root / "a.mkv").exists())
        self.assertFalse(self.tmp_file().exists())
        self.assertIn("低于要求的", self.store.last(self.src)["reason"])

    def test_good_prediction_proceeds(self):
        # 400 bps -> 500 字节，省 50%
        _ctx, outcome = self.run_with_choice(adaptive.CrfChoice(30, 400, True, 96.0))
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual((self.root / "a.mkv").read_bytes(), self.encoded)

    def test_unreachable_target_skips(self):
        _ctx, outcome = self.run_with_choice(
            adaptive.CrfChoice(16, 100, False, 88.0))
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertTrue(self.src.exists())
        self.assertIn("低于目标", self.store.last(self.src)["reason"])

    def test_unreachable_target_can_be_allowed(self):
        self.cfg["adaptive"]["require_target"] = False
        _ctx, outcome = self.run_with_choice(
            adaptive.CrfChoice(16, 100, False, 88.0))
        self.assertEqual(outcome, pipeline.OK)

    def test_unknown_bitrate_does_not_block(self):
        """预估不出来时照旧编码，别因为拿不到数就少压文件。"""
        _ctx, outcome = self.run_with_choice(adaptive.CrfChoice(30, 0, True, 96.0))
        self.assertEqual(outcome, pipeline.OK)


class FractionalCrfTests(PipelineTestCase):
    """0.25 级 CRF 要一路走到 ffmpeg 命令和记录里，不能在半路被截成整数。"""

    def setUp(self):
        super().setUp()
        # 音轨算 0，预估体积才不会把这些用例全判成"不值得压"
        self.info = make_info(path=self.src, size=1000, duration=10.0,
                              audio_codecs=[], audio_bitrates=[],
                              audio_channels=[])

    def capture(self, choice=None):
        seen = {}

        def run_encode(cmd, duration, cb=None):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(self.encoded)

        ctx = self.ctx()
        p1, p2, p3 = self.patches(run_encode)
        if choice is None:
            with p1, p2, p3:
                pipeline.process_file(ctx, self.src)
        else:
            self.cfg["adaptive"]["enabled"] = True
            with p1, p2, p3, mock.patch.object(pipeline.adaptive,
                                              "pick_crf",
                                              return_value=choice):
                pipeline.process_file(ctx, self.src)
        return seen["cmd"]

    def test_fractional_crf_from_the_config_is_honoured(self):
        self.cfg["encode"]["av1"]["crf"] = 20.5
        cmd = self.capture()
        self.assertIn("crf=20.5", " ".join(cmd),
                      "a fixed crf of 20.5 must not be truncated to 20")

    def test_fractional_crf_from_the_search_reaches_ffmpeg(self):
        cmd = self.capture(adaptive.CrfChoice(30.25, 400, True, 96.0))
        self.assertIn("crf=30.25", " ".join(cmd))

    def test_record_keeps_integers_integral(self):
        self.capture(adaptive.CrfChoice(30, 400, True, 96.0))
        self.assertEqual(self.store.last(self.src)["crf"], 30)

    def test_record_keeps_the_fraction(self):
        self.capture(adaptive.CrfChoice(30.25, 400, True, 96.0))
        self.assertEqual(self.store.last(self.src)["crf"], 30.25)


class TargetRecheckTests(PipelineTestCase):
    """自适应产物要按 target_vmaf 复核全片抽检：欠账必须留痕，但不否决产物。"""

    def setUp(self):
        super().setUp()
        self.cfg["adaptive"]["enabled"] = True
        self.cfg["adaptive"]["target_vmaf"] = 95.0
        self.info = make_info(path=self.src, size=1000, duration=10.0,
                              audio_codecs=[], audio_bitrates=[],
                              audio_channels=[])
        self.logs = []
        self.handler = logging.Handler()
        self.handler.emit = self.logs.append
        logging.getLogger("vcompress").addHandler(self.handler)

    def tearDown(self):
        logging.getLogger("vcompress").removeHandler(self.handler)
        super().tearDown()

    def messages(self):
        return "\n".join(r.getMessage() for r in self.logs)

    def run_with_spot(self, spot):
        ctx = self.ctx()
        p1, p2, _p3 = self.patches(self.fake_encode())
        run_all = mock.Mock(return_value=spot)
        with p1, p2, \
                mock.patch.object(pipeline.verify, "run_all", run_all), \
                mock.patch.object(pipeline.adaptive, "pick_crf",
                                  return_value=adaptive.CrfChoice(
                                      30, 400, True, 95.02)):
            outcome = pipeline.process_file(ctx, self.src)
        return outcome

    def test_below_target_warns_but_still_passes(self):
        outcome = self.run_with_spot((94.99, 94.39))
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual((self.root / "a.mkv").read_bytes(), self.encoded,
                         "产物过了 [verify] 闸门，仍按通过落地")
        msg = self.messages()
        self.assertIn("没达到自适应目标", msg)
        self.assertIn("94.99", msg)
        self.assertIn("94.39", msg)
        self.assertIn("95.02", msg)

    def test_meeting_target_stays_quiet(self):
        outcome = self.run_with_spot((95.5, 95.1))
        self.assertEqual(outcome, pipeline.OK)
        self.assertNotIn("没达到自适应目标", self.messages())

    def test_fixed_crf_has_no_target_to_recheck(self):
        self.cfg["adaptive"]["enabled"] = False
        ctx = self.ctx()
        p1, p2, _p3 = self.patches(self.fake_encode())
        run_all = mock.Mock(return_value=(93.5, 91.0))
        with p1, p2, mock.patch.object(pipeline.verify, "run_all", run_all):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.OK)
        self.assertNotIn("没达到自适应目标", self.messages())


class OutputDirTests(PipelineTestCase):
    """-o/--output：产物落到指定目录，覆盖保护规则在目标目录里照常生效。"""

    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.out.mkdir()

    def test_output_lands_in_the_given_directory(self):
        _ctx, outcome = self.run_one(self.fake_encode(), output_dir=self.out)
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual((self.out / "a.mkv").read_bytes(), self.encoded)
        self.assertFalse((self.root / "a.mkv").exists(),
                         "源目录里不该出现产物")
        self.assertFalse(self.src.exists(), "默认非 keep：原文件压缩后删除")

    def test_keep_mode_keeps_source_and_writes_copy_to_output_dir(self):
        _ctx, outcome = self.run_one(self.fake_encode(), keep=True,
                                     output_dir=self.out)
        self.assertEqual(outcome, pipeline.OK)
        self.assertTrue(self.src.exists())
        self.assertTrue((self.out / "a.mkv").exists())

    def test_name_conflict_falls_back_inside_the_output_dir(self):
        (self.out / "a.mkv").write_bytes(b"precious")
        _ctx, outcome = self.run_one(self.fake_encode(), output_dir=self.out)
        self.assertEqual(outcome, pipeline.OK)
        self.assertEqual((self.out / "a.mkv").read_bytes(), b"precious")
        self.assertEqual((self.out / "a.av1.mkv").read_bytes(), self.encoded)
        self.assertFalse((self.root / "a.av1.mkv").exists(),
                         "备用名不该落回源目录")


class ContextTests(unittest.TestCase):
    def test_context_rejects_unknown_attributes(self):
        """dataclass 而不是空壳类：拼错属性名要立刻炸，而不是运行时才发现。"""
        ctx = RunContext(cfg=make_cfg(), tools=helpers.FakeTools(),
                         caps=caps.Caps(), store=None,
                         outputs=files.OutputRegistry())
        with self.assertRaises(Exception):
            ctx.dry_runn = True

    def test_derived_properties(self):
        cfg = make_cfg()
        ctx = RunContext(cfg=cfg, tools=helpers.FakeTools(),
                         caps=caps.Caps(), store=None,
                         outputs=files.OutputRegistry(), encoder="x265")
        self.assertEqual(ctx.out_ext, ".mkv")
        self.assertEqual(ctx.enc_cfg["codec"], "libx265")
        self.assertEqual(ctx.marker, "vcompress-v1")
        self.assertEqual(ctx.lossless_codec, "libx264")

    def test_adaptive_can_be_disabled_from_either_side(self):
        cfg = make_cfg()
        base = dict(cfg=cfg, tools=helpers.FakeTools(), caps=caps.Caps(),
                    store=None, outputs=files.OutputRegistry())
        self.assertTrue(RunContext(**base, adaptive=True).adaptive_enabled)
        self.assertFalse(RunContext(**base, adaptive=False).adaptive_enabled)
        cfg["adaptive"]["enabled"] = False
        self.assertFalse(RunContext(**base, adaptive=True).adaptive_enabled)


if __name__ == "__main__":
    unittest.main()
