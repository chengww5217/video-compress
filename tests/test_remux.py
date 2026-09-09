import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tempfile
import unittest
import shlex
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import helpers
from helpers import make_cfg, make_info
from vcompress import (adaptive, caps, encode, files, pipeline, proc, remux,
                       verify)
from vcompress.pipeline import RunContext, RunStats
from vcompress.results import ResultStore


class KeyframeScanTests(unittest.TestCase):
    """关键帧间隔的量法：包级扫描、含片头片尾、单个关键帧。"""

    def setUp(self):
        proc.clear_cancel()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.src = self.root / "a.mkv"
        self.src.write_bytes(b"x" * 10)

    def tearDown(self):
        proc.clear_cancel()
        self.tmpdir.cleanup()

    def scan(self, csv_text, rc=0, err=""):
        with mock.patch.object(remux.proc, "run_managed",
                               return_value=(rc, csv_text, err)):
            return remux.keyframe_timestamps(helpers.FakeTools(), self.src)

    def test_csv_is_parsed_and_non_keyframes_dropped(self):
        out = self.scan("0.000000,0.000000,K__\n"
                        "0.040000,0.040000,___\n"
                        "10.000000,10.000000,K__\n"
                        "10.040000,10.040000,___\n"
                        "20.000000,20.000000,K__\n")
        self.assertEqual(out, [0.0, 10.0, 20.0])

    def test_pts_na_falls_back_to_dts(self):
        """AVI 这类容器只有 dts，只看 pts 会把关键帧全读丢。"""
        out = self.scan("N/A,0.000000,K__\n"
                        "N/A,0.040000,___\n"
                        "N/A,10.000000,K__\n")
        self.assertEqual(out, [0.0, 10.0])

    def test_both_timestamps_na_is_skipped(self):
        out = self.scan("N/A,N/A,K__\n5.000000,5.000000,K__\n")
        self.assertEqual(out, [5.0])

    def test_probe_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self.scan("", rc=1, err="bogus file")

    def test_gaps_include_head_and_tail(self):
        # 关键帧在 5s 和 10s，片长 30s：片尾 20s 是最大缺口
        gap = remux.max_keyframe_gap([5.0, 10.0], 30.0)
        self.assertEqual(gap, 20.0)

    def test_single_keyframe_means_the_whole_file(self):
        self.assertEqual(remux.max_keyframe_gap([0.0], 600.0), 600.0)

    def test_head_gap_counts(self):
        # 关键帧 12s 才出现：片头这 12s 就是最大缺口
        self.assertEqual(remux.max_keyframe_gap([12.0, 22.0, 32.0], 32.0),
                         12.0)

    def test_unknown_duration_falls_back_to_last_keyframe(self):
        self.assertEqual(remux.max_keyframe_gap([0.0, 30.0, 61.0], 0.0), 31.0)

    def test_no_keyframes_is_zero(self):
        self.assertEqual(remux.max_keyframe_gap([], 100.0), 0.0)


class KeyintTests(unittest.TestCase):
    def test_frames_come_from_fps_times_seconds(self):
        self.assertEqual(remux.keyint_frames(30.0, 5.0), 150)

    def test_zero_fps_assumes_25(self):
        self.assertEqual(remux.keyint_frames(0.0, 4.0), 100)

    def test_keyint_is_clamped_for_very_high_framerates(self):
        # 夹小只是让关键帧更密（承诺更有保障），夹大才会失约
        self.assertLessEqual(remux.keyint_frames(120.0, 5.0), remux.MAX_KEYINT)

    def test_svtav1_replaces_existing_keyint(self):
        cfg = make_cfg()["encode"]["av1"]
        got = remux.bound_keyint_cfg(cfg, 24.0, 5.0)
        joined = " ".join(shlex.split(got["extra"]))
        self.assertIn("keyint=120", joined)

    def test_x265_gets_keyint_added(self):
        cfg = make_cfg()["encode"]["x265"]
        got = remux.bound_keyint_cfg(cfg, 25.0, 5.0)
        self.assertIn("keyint=125", got["extra"])

    def test_unknown_codec_is_left_alone(self):
        cfg = {"codec": "libaom-av1", "extra": "-cpu-used 4"}
        self.assertEqual(remux.bound_keyint_cfg(cfg, 25.0, 5.0), cfg)


class PlanTests(unittest.TestCase):
    """决策树：GOP 超限重编、老容器换封、都没问题不动。"""

    def setUp(self):
        self.cfg = make_cfg()

    def plan(self, max_gap, **kw):
        return remux.plan_remux(make_info(**kw), self.cfg, max_gap)

    def test_long_gap_needs_reencode(self):
        plan = self.plan(300.0)
        self.assertEqual(plan.strategy, remux.GOP)
        self.assertEqual(plan.keyint, 150)  # 30fps × 5s
        self.assertIn("300", plan.reason)

    def test_fine_gap_and_modern_container_is_left_alone(self):
        plan = self.plan(2.0)
        self.assertEqual(plan.strategy, "")
        self.assertIn("没有问题", plan.reason)

    def test_legacy_container_gets_stream_copy(self):
        plan = self.plan(2.0, container=".avi")
        self.assertEqual(plan.strategy, remux.COPY)

    def test_interlaced_sources_never_get_reencoded(self):
        plan = self.plan(300.0, field_order="tt")
        self.assertEqual(plan.strategy, "")
        self.assertIn("交错", plan.reason)

    def test_copy_requires_a_different_container(self):
        self.cfg["encode"]["output_container"] = ".avi"
        plan = self.plan(2.0, container=".avi")
        self.assertEqual(plan.strategy, "")

    def test_copy_requires_a_compatible_target(self):
        # flv1 装不进 mp4，流复制会整个失败
        self.cfg["encode"]["output_container"] = ".mp4"
        plan = self.plan(2.0, container=".flv", video_codec="flv1",
                         codec_family="flv1")
        self.assertEqual(plan.strategy, "")
        self.assertIn("装不进", plan.reason)

    def test_mkv_takes_anything(self):
        self.cfg["encode"]["output_container"] = ".mkv"
        plan = self.plan(2.0, container=".wmv", video_codec="wmv3")
        self.assertEqual(plan.strategy, remux.COPY)


class PredictedSizeTests(unittest.TestCase):
    def test_video_plus_copied_audio(self):
        info = make_info(duration=100.0, audio_bitrates=[128_000])
        self.assertEqual(remux.predicted_size(info, 1_000_000),
                         (1_000_000 + 128_000) * 100 // 8)

    def test_unknown_audio_bitrate_means_no_prediction(self):
        info = make_info(duration=100.0, audio_bitrates=[0])
        self.assertIsNone(remux.predicted_size(info, 1_000_000))

    def test_no_duration_no_prediction(self):
        info = make_info(duration=0.0, audio_bitrates=[128_000])
        self.assertIsNone(remux.predicted_size(info, 1_000_000))


class AdaptiveCfgTests(unittest.TestCase):
    def test_remux_target_replaces_the_compression_target(self):
        cfg = make_cfg()
        cfg["adaptive"]["target_vmaf"] = 95.0
        cfg["remux"]["target_vmaf"] = 98.0
        ad = remux.adaptive_cfg(cfg)
        self.assertEqual(ad["target_vmaf"], 98.0)
        # 其余抽样口径必须原样沿用——搜索和抽检共用同一套取样代码
        self.assertEqual(ad["sample_count"], cfg["adaptive"]["sample_count"])
        self.assertEqual(cfg["adaptive"]["target_vmaf"], 95.0,
                         "原配置不能被就地改掉")


class CheckGrowthTests(unittest.TestCase):
    def test_within_limit_passes_even_when_bigger(self):
        ok, msg = verify.check_growth(1000, 1080, 0.10)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_over_limit_is_rejected_with_numbers(self):
        ok, msg = verify.check_growth(1000, 1200, 0.10)
        self.assertFalse(ok)
        self.assertIn("上限", msg)
        self.assertIn("20.0%", msg)


class CopyCmdTests(unittest.TestCase):
    def test_copy_cmd_keeps_bytes_and_adds_the_marker(self):
        src = Path("/v/a.avi")
        dst = Path("/v/a.remux.mkv")
        cmd = encode.build_copy_cmd("ffmpeg", src, dst,
                                    ["-c:s:0", "srt"], "vcompress-remux-v1",
                                    "ripped by someone")
        joined = " ".join(cmd)
        self.assertIn("-c:v copy", joined)
        self.assertIn("-c:a copy", joined)
        self.assertIn("-map_metadata 0", joined)
        self.assertIn("-map_chapters 0", joined)
        # mkv 系要带上附件映射，mp4 系的时间码这类 data 流要丢掉
        self.assertIn("-map 0:t?", joined)
        self.assertIn("-dn", joined)
        self.assertIn("ripped by someone | vcompress-remux-v1", joined)
        self.assertIn("-progress pipe:1", joined,
                      "流复制也要走 run_encode 的进度解析")
        self.assertEqual(cmd[-1], str(dst))
        self.assertNotIn("-crf", joined)


class MergeDictParamTests(unittest.TestCase):
    def test_same_key_is_replaced_not_duplicated(self):
        got = encode.merge_dict_param("-svtav1-params scd=1:keyint=257",
                                      "-svtav1-params", "keyint", "120")
        tokens = shlex.split(got)
        self.assertEqual(tokens.count("-svtav1-params"), 1)
        self.assertIn("scd=1", tokens[-1].split(":"))
        self.assertIn("keyint=120", tokens[-1].split(":"))
        self.assertNotIn("keyint=257", tokens[-1].split(":"))

    def test_option_is_appended_when_missing(self):
        got = encode.merge_dict_param("-x265-params aq-mode=3",
                                      "-x265-params", "keyint", "100")
        tokens = shlex.split(got)
        self.assertEqual(tokens, ["-x265-params", "aq-mode=3:keyint=100"])


class RemuxPipelineTestCase(unittest.TestCase):
    """重封装流程编排的替身测试，套路与 test_pipeline 相同。"""

    def setUp(self):
        proc.clear_cancel()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.src = self.root / "a.mkv"
        self.src.write_bytes(b"o" * 1000)
        self.cfg = make_cfg()
        self.cfg["skip"]["min_size_mb"] = 0
        self.cfg["adaptive"]["enabled"] = True
        self.store = ResultStore(self.root / "results.jsonl")
        # 「已经压过了」的文件是重封装模式的主要客户；音轨清零让体积预估
        # 只反映视频轨（test_pipeline 的预估测试同款手法）
        self.info = make_info(path=self.src, size=1000, duration=600.0,
                              comment="vcompress-v1", audio_codecs=[],
                              audio_bitrates=[], audio_channels=[])
        self.encoded = b"e" * 1050

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
            remux=True,
            **kw,
        )

    def tmp_file(self):
        return files.tmp_path(self.src, ".mkv")

    def run_remux(self, stamps_src, stamps_out=None, choice=None,
                  run_encode=None, verify_ok=True, dry_run=False, **ctx_kw):
        """替身跑一遍：源的关键帧间隔稀疏、产物按需要另给一套。"""
        out_info = make_info(path=self.tmp_file(), video_codec="av1",
                             size=len(self.encoded), duration=600.0,
                             comment="vcompress-remux-v1")

        def probe_side_effect(tools, path):
            return self.info if Path(path) == self.src else out_info

        def default_encode(cmd, duration, cb=None):
            Path(cmd[-1]).write_bytes(self.encoded)
            if cb:
                cb(50.0, 1.0, 2.0)

        run_all = (mock.Mock(return_value=(98.6, 98.1)) if verify_ok
                   else mock.Mock(side_effect=verify.VerifyError("vmaf 40")))
        stamps_out = stamps_out if stamps_out is not None else stamps_src

        def stamps(tools, path):
            return stamps_src if Path(path) == self.src else stamps_out

        patches = [
            mock.patch.object(pipeline.probe, "probe",
                              side_effect=probe_side_effect),
            mock.patch.object(pipeline.encode, "run_encode",
                              run_encode or default_encode),
            mock.patch.object(pipeline.verify, "run_all", run_all),
            mock.patch.object(pipeline.remux, "keyframe_timestamps",
                              side_effect=stamps),
        ]
        if choice is not None:
            patches.append(mock.patch.object(pipeline.adaptive, "pick_crf",
                                             return_value=choice))
        ctx = self.ctx(dry_run=dry_run, **ctx_kw)
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return ctx, pipeline.process_file(ctx, self.src)


class RemuxEntryTests(RemuxPipelineTestCase):
    def test_already_remuxed_marker_skips(self):
        self.info.comment = "vcompress-remux-v1"
        ctx = self.ctx()
        with mock.patch.object(pipeline.probe, "probe",
                              return_value=self.info):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        rec = self.store.last(self.src)
        self.assertEqual(rec["mode"], "remux")
        self.assertIn("重封装标记", rec["reason"])

    def test_interlaced_source_is_not_touched(self):
        self.info.field_order = "tt"
        ctx = self.ctx()
        with mock.patch.object(pipeline.probe, "probe",
                              return_value=self.info):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("交错", self.store.last(self.src)["reason"])

    def test_compressible_file_still_gets_compressed_with_bounded_keyint(self):
        """该压的照常压，但 keyint 一并收紧——不然压完还得为快进再编一遍。"""
        self.info.comment = ""  # 没有「已压过」标记 → 会走正常压缩
        seen = {}

        def run_encode(cmd, duration, cb=None):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(self.encoded)

        ctx, outcome = self.run_remux(
            [], [], choice=adaptive.CrfChoice(30, 1, True, 98.2),
            run_encode=run_encode)
        self.assertEqual(outcome, pipeline.OK)
        joined = " ".join(seen["cmd"])
        self.assertIn("keyint=", joined)
        # 正常压缩的产物还是打普通标记
        self.assertIn("comment=vcompress-v1", joined)
        rec = self.store.last(self.src)
        self.assertEqual(rec["mode"], "remux")

    def test_dry_run_reports_the_plan_without_touching_anything(self):
        ctx = self.ctx(dry_run=True)
        with mock.patch.object(pipeline.probe, "probe",
                               return_value=self.info), \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  return_value=[0.0, 300.0]):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.DRYRUN)
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.mkv"])
        self.assertIsNone(self.store.last(self.src))

    def test_nothing_to_fix_is_a_skip_with_a_reason(self):
        ctx = self.ctx()
        # 间隔要覆盖到片尾：最大间隔的计算包含最后一个关键帧到结束那一段
        dense = [float(t) for t in range(0, 601, 5)]
        with mock.patch.object(pipeline.probe, "probe",
                               return_value=self.info), \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  return_value=dense):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("没有问题", self.store.last(self.src)["reason"])

    def test_unreadable_keyframes_is_a_skip_not_a_failure(self):
        ctx = self.ctx()
        with mock.patch.object(pipeline.probe, "probe",
                               return_value=self.info), \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  side_effect=RuntimeError("ffprobe died")):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("扫关键帧失败", self.store.last(self.src)["reason"])


class GopRemuxTests(RemuxPipelineTestCase):
    # 产物侧的关键帧密度要真达标：覆盖到片尾、间隔 ≤ 4s
    DENSE = [float(t) for t in range(0, 601, 4)]

    def test_success_path_commits_with_growth_and_strategy(self):
        # 源最长间隔 300s（0 和 300 两帧之间），产物间隔 4s 达标
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=adaptive.CrfChoice(18, 1, True, 98.3))
        self.assertEqual(outcome, pipeline.OK)
        self.assertTrue((self.root / "a.av1.mkv").exists())
        self.assertFalse(self.src.exists(), "默认删除原文件（keep=False）")
        rec = self.store.last(self.src)
        self.assertEqual(rec["action"], "ok")
        self.assertEqual(rec["mode"], "remux")
        self.assertEqual(rec["strategy"], "gop")
        self.assertEqual(rec["crf"], 18)
        self.assertGreater(rec["growth"], 0, "1050/1000 = +5% 应记为增长")

    def test_unreachable_target_leaves_the_file_alone(self):
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=adaptive.CrfChoice(16, 100, False, 90.0))
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.tmp_file().exists())
        self.assertIn("重封装目标", self.store.last(self.src)["reason"])

    def test_predicted_growth_over_the_limit_skips_before_encoding(self):
        # 600s × 100bps / 8 = 7500 字节 >> 1000 × 1.1
        encoded = []

        def run_encode(cmd, duration, cb=None):
            encoded.append(cmd)

        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=adaptive.CrfChoice(18, 100, True, 98.3),
            run_encode=run_encode)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertEqual(encoded, [], "预估就出局了，不该起编码")
        self.assertTrue(self.src.exists())
        self.assertIn("上限", self.store.last(self.src)["reason"])

    def test_search_failure_does_not_fall_back_to_fixed_crf(self):
        ctx = self.ctx()
        out_info = make_info(path=self.tmp_file(), video_codec="av1",
                             size=len(self.encoded), duration=600.0)
        with mock.patch.object(pipeline.probe, "probe",
                               return_value=self.info), \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  return_value=[0.0, 300.0]), \
                mock.patch.object(pipeline.adaptive, "pick_crf",
                                  return_value=None):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("不回退固定 CRF", self.store.last(self.src)["reason"])

    def test_no_adaptive_search_means_no_reencode(self):
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=None, adaptive=False)
        self.assertEqual(outcome, pipeline.SKIPPED)
        self.assertIn("自适应", self.store.last(self.src)["reason"])

    def test_output_still_too_sparse_fails_verification(self):
        """编码器没兑现 keyint 承诺就不能交付「修了但没修好」的产物。"""
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], [0.0, 300.0],
            choice=adaptive.CrfChoice(18, 1, True, 98.3))
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertTrue(self.src.exists())
        self.assertFalse(self.tmp_file().exists())
        self.assertIn("keyint 参数没有生效",
                      self.store.last(self.src)["error"])

    def test_verify_failure_keeps_the_original(self):
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=adaptive.CrfChoice(18, 1, True, 98.3),
            verify_ok=False)
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertTrue(self.src.exists())
        self.assertIn("vmaf 40", self.store.last(self.src)["error"])

    def test_keep_mode_leaves_the_original(self):
        _ctx, outcome = self.run_remux(
            [0.0, 300.0], self.DENSE,
            choice=adaptive.CrfChoice(18, 1, True, 98.3), keep=True)
        self.assertEqual(outcome, pipeline.OK)
        self.assertTrue(self.src.exists())
        self.assertTrue((self.root / "a.av1.mkv").exists())


class CopyRemuxTests(RemuxPipelineTestCase):
    def setUp(self):
        super().setUp()
        # 容器老旧但关键帧密度正常 → 流复制换封。片子改短，密集的关键帧
        # 才能同时覆盖片头片尾（最大间隔的计算含这两段）
        self.info.container = ".avi"
        self.info.duration = 4.0
        self.encoded = b"e" * 1010  # +1% 的容器开销
        self.dense = [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_success_path_copies_streams(self):
        seen = {}

        def run_encode(cmd, duration, cb=None):
            seen["cmd"] = cmd
            Path(cmd[-1]).write_bytes(self.encoded)

        ctx = self.ctx()
        out_info = make_info(path=self.tmp_file(), video_codec="h264",
                             size=len(self.encoded), duration=4.0,
                             container=".mkv")
        with mock.patch.object(pipeline.probe, "probe",
                               side_effect=lambda tools, path:
                               self.info if Path(path) == self.src
                               else out_info), \
                mock.patch.object(pipeline.encode, "run_encode", run_encode), \
                mock.patch.object(pipeline.remux, "verify_copy") as vcopy, \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  return_value=self.dense):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.OK)
        joined = " ".join(seen["cmd"])
        self.assertIn("-c:v copy", joined)
        self.assertIn("-c:a copy", joined)
        self.assertIn("vcompress-remux-v1", joined)
        vcopy.assert_called_once()
        self.assertTrue((self.root / "a.remux.mkv").exists(),
                        "流复制产物用 .remux 后缀，不冒充编码产物")
        rec = self.store.last(self.src)
        self.assertEqual(rec["strategy"], "copy")
        self.assertEqual(rec["mode"], "remux")

    def test_verify_failure_discards_the_output(self):
        def run_encode(cmd, duration, cb=None):
            Path(cmd[-1]).write_bytes(self.encoded)

        ctx = self.ctx()
        out_info = make_info(path=self.tmp_file(), video_codec="h264",
                             size=len(self.encoded), duration=4.0)
        with mock.patch.object(pipeline.probe, "probe",
                               side_effect=lambda tools, path:
                               self.info if Path(path) == self.src
                               else out_info), \
                mock.patch.object(pipeline.encode, "run_encode", run_encode), \
                mock.patch.object(pipeline.remux, "verify_copy",
                                  side_effect=verify.VerifyError(
                                      "时长差了 3.00s")), \
                mock.patch.object(pipeline.remux, "keyframe_timestamps",
                                  return_value=self.dense):
            outcome = pipeline.process_file(ctx, self.src)
        self.assertEqual(outcome, pipeline.FAILED)
        self.assertTrue(self.src.exists())
        self.assertFalse((self.root / "a.remux.mkv").exists())
        self.assertIn("时长差了", self.store.last(self.src)["error"])


class RemuxCacheTests(RemuxPipelineTestCase):
    def test_mode_mismatch_is_not_a_cache_hit(self):
        """上次压缩过的文件，重封装模式必须重新查（这正是它的活）。"""
        self.store.add("ok", self.info, new_size=900, mode="compress")
        self.assertIsNotNone(self.store.is_cached(self.src, "compress"))
        self.assertIsNone(self.store.is_cached(self.src, "remux"))

    def test_same_mode_is_cached(self):
        self.store.add("ok", self.info, new_size=900, mode="remux")
        self.assertIsNotNone(self.store.is_cached(self.src, "remux"))
        self.assertIsNone(self.store.is_cached(self.src, "compress"))

    def test_old_records_count_as_compress_mode(self):
        self.store.add("ok", self.info, new_size=900)
        self.assertIsNotNone(self.store.is_cached(self.src, "compress"))
        self.assertIsNone(self.store.is_cached(self.src, "remux"))


if __name__ == "__main__":
    unittest.main()
