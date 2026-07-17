# 当前模块清单

本文档基于只读扫描 `D:\zjic2026\cadscene_workbench\project` 得出。`project/` 只是旧项目参考代码，不是新项目的一部分；新项目不得 `import project`、不得 `import cadvideo`、不得依赖 `project/` 路径。缺失 `out/` 历史产物时，只依据源码、tests、docs 中可见信息判断。

## 主线必须迁移

这些模块构成 `v0.1-sfm-workbench` 的 SfM-CAD 产品化主线，应迁入 `cadscene_workbench/cadscene/` 并整理为清晰 API。

| 旧路径 | 迁移理由 | 新职责建议 |
|---|---|---|
| `project/cadvideo/sfm_align/segmentation.py` | DINOv3 语义分割主线；旧 CLI `segment_video.py` 调用它。 | `cadscene/sfm/segmentation.py` |
| `project/cadvideo/sfm_align/sfm.py` | pycolmap/COLMAP SfM 重建、抽帧、mask、轨迹和点云导出。 | `cadscene/sfm/reconstruction.py` |
| `project/cadvideo/sfm_align/align.py` | SfM trajectory parsing、global sim3、segment anchoring、对齐后相机状态生成。 | `cadscene/alignment/aligner.py`，其中 sim3 拆到 `cadscene/core/sim3.py` |
| `project/cadvideo/sfm_align/quality.py` | quality module 核心风险评分、权重归一、关键帧推荐。 | `cadscene/alignment/quality.py` |
| `project/cadvideo/sfm_align/viewer_scene.py` | SfM 点云、global track、anchored track、suggestions 的 viewer scene 组装。 | `cadscene/viewer/export_scene.py` |
| `project/cadvideo/sfm_align/road_surface.py` | road surface diagnostics：centerline station、道路点提取、平面/高程 profile 诊断。 | `cadscene/diagnostics/road_surface.py` |
| `project/cadvideo/sfm_align/diagnostics.py` | 点云统计、keyframe pose residual、camera z profile、geometry report 支撑逻辑。 | `cadscene/diagnostics/geometry.py` 与 `pose_residual.py` |
| `project/cadvideo/sfm_align/keyframe_downsample.py` | 120f/180f/240f keyframe density ablation，明确跳过 `algorithm_prediction`。 | `cadscene/alignment/keyframes_downsample.py` |
| `project/cadvideo/sfm_align/overlay.py` | faded overlay render 相关逻辑。 | `cadscene/rendering/overlay.py` |

对应 CLI 也必须保留为薄入口：

- `project/cadvideo/pipeline/segment_video.py`
- `project/cadvideo/pipeline/run_video_sfm.py`
- `project/cadvideo/pipeline/align_sfm_to_cad.py`
- `project/cadvideo/pipeline/evaluate_sfm_alignment_quality.py`
- `project/cadvideo/pipeline/export_sfm_scene_for_viewer.py`
- `project/cadvideo/pipeline/analyze_sfm_road_surface.py`
- `project/cadvideo/pipeline/downsample_web_keyframes.py`
- `project/cadvideo/pipeline/compare_keyframe_ablation.py`

`project/web_camera_viewer/` 也是主线展示能力的一部分，需迁入 `cadscene_workbench/apps/web_camera_viewer/`。旧 viewer 当前是 legacy script 版本：`index.html + paths.js + fallback.js + viewer_legacy.js + vendor/`，功能包含 quality timeline、suggestions、SfM point cloud、global/anchored 双轨迹、当前 track 覆盖 anchored path。

## 可复用但需要重写/抽象

这些代码含主线需要的概念，但目前位置和依赖边界不适合直接迁移。

| 旧路径 | 可复用内容 | 重写要求 |
|---|---|---|
| `project/cadvideo/pipeline/pose_align_viewer.py` | `CameraState`、`CadBundle`、CAD loader、投影、坐标转换、overlay 绘制。 | 拆到 `core/camera.py`、`cad/loader.py`、`cad/projection.py`、`rendering/styles.py`；去掉交互 UI 和旧 CLI 编排。 |
| `project/cadvideo/pipeline/camera_pose_track_groundplane.py` | `CenterlineModel`、`camera_state_cad_xy`、CAD xy / camera xy 工具。 | 抽到 `cad/centerline.py` 与 `cad/coordinates.py`，不得保留 tracker 依赖。 |
| `project/cadvideo/video_path/anchors.py` | `confirmed_keyframes`、web camera → python state、pitch 反号、`algorithm_prediction` 过滤。 | 抽到 `alignment/keyframes.py` 和 `core/coordinates.py`；主线只接受 manual/confirmed 锚点。 |
| `project/cadvideo/video_path/centerline.py` | station/lateral 投影与反投影。 | 合并到 `cad/centerline.py`，用清晰类型表达 `station_m` / `lateral_m`。 |
| `project/cadvideo/video_path/render.py` | 从路径行生成 `CameraState`、视频 overlay 编排经验。 | overlay 主线迁到 `rendering/overlay.py`，CLI 只调用渲染服务。 |
| `project/cadvideo/io_utils.py` 和 `project/cadvideo/cad/*` | CAD RoadLine、DXF/DWG 导出链路、中文路径安全 IO。 | 迁到 `cad/loader.py`、`core/io.py`；写文件集中通过 `ArtifactManager`。 |

## 仅作为参考

这些模块或文档对理解旧实验有价值，但不进入 v0.1 主线。

- `project/docs/场景二_SfM语义分割路线.md`：记录 SfM 主线、quality、viewer scene、road diagnostics、120f/180f/240f ablation、240f demo 命令。迁移文档可引用其结论，但不得依赖其中 `out/` 产物存在。
- `project/docs/web_camera_viewer_使用说明.md`、`project/web_camera_viewer/README.md`：迁移 viewer 功能说明和 URL 参数语义。
- `project/cadvideo/pipeline/preview_sfm_cloud.py`：点云预览可作为 diagnostics/viewer 的参考工具，不作为 v0.1 必备 CLI，除非后续需要单独保留。
- `project/cadvideo/pipeline/serve_web_viewer.py`：HTTP Range 服务逻辑可参考，迁入 `cadscene/cli/serve_viewer.py`。
- `project/cadvideo/pipeline/export_web_viewer_assets.py`：旧 hygs viewer 资源导出逻辑可参考，但新项目应通过 dataset config 与 runs manifest 管理。

## 废弃，不迁移到主线

这些方法只归档，不进入 `cadscene/` 主线，不允许被主线 import。

- CAD-driven ROI-LK-PnP trackers：
  - `project/cadvideo/pipeline/camera_pose_track.py`
  - `project/cadvideo/pipeline/camera_pose_track_groundplane.py` 中 tracker 相关逻辑
  - `project/cadvideo/pipeline/track_next_segment.py`
- visible-window tracker：
  - `project/cadvideo/pipeline/camera_pose_track_visible_window.py`
  - `project/cadvideo/pipeline/render_visible_window_overlay.py`
  - `project/tests/test_visible_window_diagnostics.py`
- multi-region tracker：
  - `project/cadvideo/pipeline/camera_pose_track_multi_region.py`
  - `project/tests/test_multi_region_sampling_diagnostics.py`
  - `project/tests/test_multi_region_anchor.py`
- old `video_path` motion prior 主线：
  - `project/cadvideo/video_path/path_model.py`
  - `project/cadvideo/video_path/motion.py`
  - `project/cadvideo/pipeline/estimate_video_camera_path.py`
  - `project/cadvideo/pipeline/estimate_video_motion_prior.py`
  - `project/cadvideo/pipeline/run_video_camera_path_ablation.py`
  - `project/cadvideo/pipeline/compare_motion_prior_with_tracker.py`
- CAD visual refine / semantic refine：
  - `project/cadvideo/video_path/cad_refine.py`
  - `project/cadvideo/pipeline/refine_video_camera_path_with_cad.py`
  - `project/cadvideo/sfm_align/semantic_refine.py`
  - `project/cadvideo/pipeline/refine_semantic.py`
  - `project/cadvideo/pipeline/visual_pose_refine.py`
- green filter / sampling diagnostics / rescue fallback：
  - `project/cadvideo/video_path/road_mask.py`
  - tracker 内的 green exclusion、sampling diagnostics、rescue fallback 逻辑。

## 不确定，需要人工确认

- `project/cadvideo/pipeline/audit_camera_model_consistency.py`：可作为坐标/前后端一致性测试参考，但是否迁为正式 CLI 需确认。
- `project/cadvideo/pipeline/convert_web_camera_track.py`、`merge_review_keyframe.py`：可能可并入 viewer/keyframe 工具，是否保留独立 CLI 需确认。
- `project/cadvideo/pipeline/hygs_prepare_realign.py`、`organize_hygs_outputs.py`、`project/cadvideo/tools/organize_project_outputs.py`：偏旧项目整理和历史 out 归档，不建议进入新主线；是否迁为一次性 legacy 工具需确认。
- `project/cadvideo/cad/*`：CAD loader/export 是否完整迁移取决于新项目第一版是否要求从原始 CAD 重新导出 road JSON，还是先消费已配置 CAD 资产。

