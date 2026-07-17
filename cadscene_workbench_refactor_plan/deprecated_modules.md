# 废弃模块归档说明

本文件列出不迁入主线的旧方法。要求：只归档，不删除；`cadscene/` 主线不得 import legacy；测试也不得要求 legacy 方法参与 v0.1 主线通过。

## CAD-driven ROI-LK-PnP trackers

旧路径：

- `project/cadvideo/pipeline/camera_pose_track.py`
- `project/cadvideo/pipeline/camera_pose_track_groundplane.py`
- `project/cadvideo/pipeline/track_next_segment.py`

废弃原因：

- 依赖 CAD ROI 采样点和局部光流，CAD 投影一旦偏移会导致采样变差，形成正反馈。
- 缺少真实 3D ego-motion，全局漂移难以被关键帧之外的帧约束。
- 旧文档中已明确 SfM 3D 骨架路线用于替代该类链式 ROI 反算。

替代主线：

- `cadscene/sfm/reconstruction.py`
- `cadscene/alignment/aligner.py`
- `cadscene/alignment/quality.py`

归档位置：

- `cadscene_workbench/legacy/old_trackers/roi_lk_pnp/`

## Visible-window tracker

旧路径：

- `project/cadvideo/pipeline/camera_pose_track_visible_window.py`
- `project/cadvideo/pipeline/render_visible_window_overlay.py`
- `project/tests/test_visible_window_diagnostics.py`

废弃原因：

- visible-window crop 和 rescue/fallback 逻辑复杂，难以维护。
- 主线需要可解释的 SfM 全局轨迹 + 分段锚定，而不是局部窗口逐帧跟踪。
- 测试覆盖可作为历史诊断参考，但不应成为新主线核心测试。

归档位置：

- `cadscene_workbench/legacy/old_trackers/visible_window/`

## Multi-region tracker

旧路径：

- `project/cadvideo/pipeline/camera_pose_track_multi_region.py`
- `project/tests/test_multi_region_sampling_diagnostics.py`
- `project/tests/test_multi_region_anchor.py`

废弃原因：

- 文件体量大、职责混杂，包含 sampling diagnostics、green filter、PnP rescue 等多套策略。
- 仍属于 CAD-driven tracker 家族，和 v0.1 SfM 主线目标相冲突。
- 可保留其中对 `algorithm_prediction` 和人工 keyframe 的理解作为参考，但不能迁入主线。

归档位置：

- `cadscene_workbench/legacy/old_trackers/multi_region/`

## Old video_path motion prior 主线

旧路径：

- `project/cadvideo/video_path/path_model.py`
- `project/cadvideo/video_path/motion.py`
- `project/cadvideo/pipeline/estimate_video_camera_path.py`
- `project/cadvideo/pipeline/estimate_video_motion_prior.py`
- `project/cadvideo/pipeline/run_video_camera_path_ablation.py`
- `project/cadvideo/pipeline/compare_motion_prior_with_tracker.py`
- `project/tests/test_video_camera_path.py`
- `project/tests/test_video_motion_prior.py`
- `project/tests/test_motion_prior_consistency.py`
- `project/tests/test_compare_motion_prior_with_tracker.py`

废弃原因：

- 旧文档说明 video_path 方向对，但使用 2D 像素仿射运动量累积/外推，长距离偏差大。
- v0.1 主线以 SfM 3D 轨迹作为骨架，motion prior 只可作为历史对照。

可复用例外：

- `video_path/anchors.py` 中 confirmed/manual 过滤、pitch 反号规则可抽象迁移。
- `video_path/centerline.py` 的 station/lateral 函数可迁入 CAD centerline。

归档位置：

- `cadscene_workbench/legacy/old_video_path/`

## refine_semantic

旧路径：

- `project/cadvideo/sfm_align/semantic_refine.py`
- `project/cadvideo/pipeline/refine_semantic.py`

废弃原因：

- 旧文档记录该方法在 hygs_1min 26kf 锚定基线上实测变差、全程漂移。
- Chamfer 目标不稳，修正量容易顶到上限，平滑后扩散成全局漂移。
- v0.1 质量模块应只读评估，不自动修改轨迹。

归档位置：

- `cadscene_workbench/legacy/deprecated_refine/semantic_refine/`

## video_path_refine_with_cad / old visual CAD refine

旧路径：

- `project/cadvideo/video_path/cad_refine.py`
- `project/cadvideo/pipeline/refine_video_camera_path_with_cad.py`
- `project/cadvideo/pipeline/visual_pose_refine.py`
- `project/tests/test_video_path_cad_refine.py`

废弃原因：

- 目标仍是旧 video_path 或局部 CAD visual refine，不属于 SfM-CAD 主线。
- 容易把“质量诊断”和“自动改位姿”混在一起，不适合第一版产品化主线。

归档位置：

- `cadscene_workbench/legacy/deprecated_refine/cad_visual_refine/`

## Green filter / sampling diagnostics / rescue fallback

旧路径：

- `project/cadvideo/video_path/road_mask.py`
- `project/cadvideo/pipeline/camera_pose_track_multi_region.py` 内 green exclusion / sampling diagnostics
- `project/cadvideo/pipeline/camera_pose_track_visible_window.py` 内 rescue fallback

废弃原因：

- 这些逻辑是旧 tracker 稳定性补丁，不是 SfM 主线基础能力。
- 容易把旧失败实验带入新架构，增加维护负担。

归档位置：

- 跟随对应 old tracker 目录归档。

