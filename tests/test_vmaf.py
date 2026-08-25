"""抽样与 VMAF 测量。"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import unittest
from pathlib import Path

from vcompress import vmaf


class PooledScoreTests(unittest.TestCase):
    def test_prefers_harmonic_mean(self):
        """算术平均会被好帧稀释，默认取调和平均。"""
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "harmonic_mean": 93.1,
                                           "min": 80}}}
        self.assertAlmostEqual(vmaf._pooled_score(data), 93.1)

    def test_explicit_pool_is_honoured(self):
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "harmonic_mean": 93.1,
                                           "min": 80}}}
        self.assertAlmostEqual(vmaf._pooled_score(data, "min"), 80)
        self.assertAlmostEqual(vmaf._pooled_score(data, "mean"), 95.5)

    def test_old_libvmaf_without_harmonic_mean_falls_back(self):
        data = {"pooled_metrics": {"vmaf": {"mean": 95.5, "min": 80}}}
        self.assertAlmostEqual(vmaf._pooled_score(data), 95.5)

    def test_named_vmaf_variant_is_found(self):
        data = {"pooled_metrics": {"integer_adm2": {"mean": 0.98},
                                   "vmaf_neg": {"mean": 91.0}}}
        self.assertAlmostEqual(vmaf._pooled_score(data), 91.0)

    def test_falls_back_to_the_only_metric(self):
        data = {"pooled_metrics": {"float_ssim": {"mean": 0.98}}}
        self.assertAlmostEqual(vmaf._pooled_score(data), 0.98)

    def test_missing_pooled_metrics_raises(self):
        with self.assertRaises(vmaf.VmafError):
            vmaf._pooled_score({})

    def test_missing_scores_raises(self):
        with self.assertRaises(vmaf.VmafError):
            vmaf._pooled_score({"pooled_metrics": {"vmaf": {"min": 1}}})

    def test_non_numeric_score_raises(self):
        with self.assertRaises(vmaf.VmafError):
            vmaf._pooled_score({"pooled_metrics": {"vmaf": {"mean": "n/a"}}})



class SegmentCmdTests(unittest.TestCase):
    def cmd(self, start, seconds=5.0):
        return vmaf.segment_cmd("ffmpeg", Path("in.mkv"), start, seconds,
                                  Path("out.mkv"))

    def seeks(self, cmd):
        i = cmd.index("-i")
        first = cmd.index("-ss")
        self.assertLess(first, i, "coarse -ss must come before -i")
        second = cmd.index("-ss", i)
        return float(cmd[first + 1]), float(cmd[second + 1])

    def test_two_stage_seek_adds_up_to_start(self):
        pre, post = self.seeks(self.cmd(600.0))
        self.assertAlmostEqual(pre + post, 600.0, places=3)
        self.assertAlmostEqual(post, vmaf.SEEK_LEADIN_S, places=3)

    def test_no_negative_seek_near_the_head(self):
        pre, post = self.seeks(self.cmd(0.5))
        self.assertEqual(pre, 0.0)
        self.assertAlmostEqual(pre + post, 0.5, places=3)

    def test_zero_start(self):
        pre, post = self.seeks(self.cmd(0.0))
        self.assertEqual((pre, post), (0.0, 0.0))

    def test_segment_is_lossless_video_only(self):
        cmd = self.cmd(10.0)
        self.assertIn("-an", cmd)
        self.assertIn("-sn", cmd)
        self.assertIn("-dn", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "0")

class SegmentStreamSelectionTests(unittest.TestCase):
    def test_attached_pictures_are_excluded(self):
        """0:v:0 会把封面图当视频流；真编码用的是 0:V，两边必须取同一条流。"""
        cmd = vmaf.segment_cmd("ffmpeg", Path("in.mp4"), 10.0, 5.0,
                                 Path("o.mkv"))
        self.assertIn("0:V:0", cmd)
        self.assertNotIn("0:v:0", cmd)

    def test_codec_is_configurable(self):
        cmd = vmaf.segment_cmd("ffmpeg", Path("in.mp4"), 10.0, 5.0,
                                 Path("o.mkv"), "ffv1")
        self.assertIn("ffv1", cmd)
        self.assertNotIn("-crf", cmd)

class FrameAlignedStartTests(unittest.TestCase):
    FPS = 24000 / 1001  # 23.976，一帧 41.708ms，mkv 的毫秒时间戳装不准

    def test_cut_sits_halfway_between_two_frames(self):
        cut = vmaf.frame_aligned_start(20.0, self.FPS)
        self.assertAlmostEqual((cut * self.FPS) % 1.0, 0.5, places=6)

    def test_nearby_starts_collapse_onto_the_same_cut(self):
        """帧边界两侧的请求点必须归到同一个切点，两个文件才不会错开一帧。"""
        below = vmaf.frame_aligned_start(20.0409, self.FPS)
        above = vmaf.frame_aligned_start(20.0625, self.FPS)
        self.assertAlmostEqual(below, above, places=6)

    def test_cut_stays_within_half_a_frame_of_the_request(self):
        for start in (17.3, 100.0, 4490.2):
            cut = vmaf.frame_aligned_start(start, self.FPS)
            self.assertLessEqual(abs(cut - start), 1.0 / self.FPS)

    def test_unknown_fps_leaves_the_start_alone(self):
        self.assertEqual(vmaf.frame_aligned_start(12.5, 0.0), 12.5)
        self.assertEqual(vmaf.frame_aligned_start(12.5, -1.0), 12.5)

    def test_never_goes_negative(self):
        self.assertEqual(vmaf.frame_aligned_start(0.0, self.FPS), 0.0)
        self.assertEqual(vmaf.frame_aligned_start(0.01, self.FPS), 0.0)
        self.assertEqual(vmaf.frame_aligned_start(-5.0, self.FPS), 0.0)

class SampleStartsTests(unittest.TestCase):
    """搜索样本必须铺开：只看中点会被一段明亮动作戏或全黑过场带偏。"""

    def test_segments_are_spread_over_the_file(self):
        starts = vmaf.sample_starts(1200.0, 10.0, 4)
        self.assertEqual([round(s) for s in starts], [145, 445, 745, 1045])

    def test_single_segment_lands_in_the_middle(self):
        self.assertEqual(vmaf.sample_starts(600.0, 30.0, 1), [285.0])

    def test_never_starts_past_the_end(self):
        for start in vmaf.sample_starts(20.0, 10.0, 4):
            self.assertLessEqual(start, 10.0)
            self.assertGreaterEqual(start, 0.0)

    def test_short_file_collapses_to_one_segment(self):
        """片子比样本还短时几段会挤在一起，别把同一段编四遍。"""
        self.assertEqual(vmaf.sample_starts(8.0, 10.0, 4), [0.0])

    def test_unknown_duration_is_safe(self):
        self.assertEqual(vmaf.sample_starts(0.0, 10.0, 4), [0.0])

    def test_count_is_clamped_to_at_least_one(self):
        self.assertEqual(len(vmaf.sample_starts(600.0, 10.0, 0)), 1)

    def test_given_fps_the_cuts_land_between_frames(self):
        """抽检那边必须帧对齐；两份实现合并后由这个参数控制。"""
        fps = 24000 / 1001
        for start in vmaf.sample_starts(1200.0, 10.0, 4, fps=fps):
            self.assertAlmostEqual((start * fps) % 1.0, 0.5, places=2)

    def test_alignment_does_not_defeat_the_deduplication(self):
        starts = vmaf.sample_starts(8.0, 10.0, 4, fps=24000 / 1001)
        self.assertEqual(len(starts), 1)

class VmafModelTests(unittest.TestCase):
    """默认模型按 1080p 观看条件训练，4K 片源用它是错的量具。"""

    def test_1080p_uses_the_default_model(self):
        self.assertEqual(vmaf.model_for(1920, 1080), "")

    def test_1440p_still_uses_the_default_model(self):
        self.assertEqual(vmaf.model_for(2560, 1440), "")

    def test_4k_switches_model(self):
        self.assertEqual(vmaf.model_for(3840, 2160), vmaf.MODEL_4K)

    def test_vertical_4k_switches_too(self):
        self.assertEqual(vmaf.model_for(2160, 3840), vmaf.MODEL_4K)

    def test_can_be_disabled(self):
        self.assertEqual(vmaf.model_for(3840, 2160, False), "")

    def test_unknown_resolution_is_safe(self):
        self.assertEqual(vmaf.model_for(0, 0), "")
        self.assertEqual(vmaf.model_for(None, None), "")

    def test_model_goes_into_the_filter_chain(self):
        self.assertIn(f"model=version={vmaf.MODEL_4K}",
                      vmaf.lavfi(0, vmaf.MODEL_4K))
        self.assertNotIn("model=", vmaf.lavfi())

class VmafLavfiTests(unittest.TestCase):
    def test_both_inputs_are_renumbered_by_frame(self):
        lavfi = vmaf.lavfi()
        self.assertEqual(lavfi.count("setpts=N/FR/TB"), 2)
        self.assertNotIn("trim", lavfi)
        self.assertIn("libvmaf", lavfi)

    def test_positive_shift_trims_the_distorted_side(self):
        lavfi = vmaf.lavfi(1)
        self.assertIn("[0:v]trim=start_frame=1,setpts", lavfi)
        self.assertIn("[1:v]setpts", lavfi)

    def test_negative_shift_trims_the_reference_side(self):
        lavfi = vmaf.lavfi(-2)
        self.assertIn("[1:v]trim=start_frame=2,setpts", lavfi)
        self.assertIn("[0:v]setpts", lavfi)


if __name__ == "__main__":
    unittest.main()
