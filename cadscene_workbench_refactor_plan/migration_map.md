# 迁移映射

本文档定义旧路径到新项目路径的目标映射。`project/` 仅作为只读参考；迁移时只能复制和整理必要逻辑，不能在新项目中 `import project`、`import cadvideo` 或依赖 `project/` 相对路径。

## 主线模块映射

| 旧路径 | 新路径 | 迁移策略 |
|---|---|---|
| `project/cadvideo/sfm_align/segmentation.py` | `cadscene_workbench/cadscene/sfm/segmentation.py` | 保留 DINOv3 分割封装；重依赖惰性导入；输出通过 ArtifactManager。 |
| `project/cadvideo/sfm_align/sfm.py` | `cadscene_workbench/cadscene/sfm/reconstruction.py` | 保留 pycolmap / COLMAP 流程；`SfmConfig` 改为 config-driven；抽出 trajectory/pointcloud IO。 |
| `project/cadvideo/sfm_align/align.py` | `cadscene_workbench/cadscene/alignment/aligner.py` | 保留 global sim3 + segment anchoring；基础类型迁到 `core/`。 |
| `project/cadvideo/sfm_align/align.py` 中 `Sim3Align` | `cadscene_workbench/cadscene/core/sim3.py` | 形成可测试的 `Sim3` apply/inverse/rotation API。 |
| `project/cadvideo/sfm_align/align.py` 中 trajectory parsing | `cadscene_workbench/cadscene/sfm/trajectory.py` | 独立解析 `camera_trajectory.json`、插值和 FOV。 |
| `project/cadvideo/sfm_align/preview.py` 中 PLY / sim3 工具 | `cadscene_workbench/cadscene/sfm/pointcloud.py` | 只迁 PLY 读取、采样、sim3 应用；预览绘图可后置。 |
| `project/cadvideo/sfm_align/quality.py` | `cadscene_workbench/cadscene/alignment/quality.py` | 保留风险组件、权重归一、自适应阈值、suggestions 选择。 |
| `project/cadvideo/sfm_align/viewer_scene.py` | `cadscene_workbench/cadscene/viewer/export_scene.py` | 保留 `sfm_viewer_scene.json` schema、点云采样、global/anchored 双轨迹。 |
| `project/cadvideo/sfm_align/road_surface.py` | `cadscene_workbench/cadscene/diagnostics/road_surface.py` | 保留 station profile、平面/高程诊断、road reliability 分类。 |
| `project/cadvideo/sfm_align/diagnostics.py` | `cadscene_workbench/cadscene/diagnostics/geometry.py` | 点云 bbox/stats、camera z profile、quality score。 |
| `project/cadvideo/sfm_align/diagnostics.py` 中 keyframe residual | `cadscene_workbench/cadscene/diagnostics/pose_residual.py` | 人工关键帧 vs global SfM / anchored path residual。 |
| `project/cadvideo/sfm_align/keyframe_downsample.py` | `cadscene_workbench/cadscene/alignment/keyframes_downsample.py` | 保留 120f/180f/240f 逻辑，确保跳过 `algorithm_prediction`。 |
| `project/cadvideo/sfm_align/overlay.py` | `cadscene_workbench/cadscene/rendering/overlay.py` | 保留 faded overlay；渲染样式单独配置。 |

## 需要拆分的旧基础模块

| 旧路径 | 新路径 | 说明 |
|---|---|---|
| `project/cadvideo/pipeline/pose_align_viewer.py` | `cadscene_workbench/cadscene/core/camera.py` | `CameraState`、相机角度、FOV、基础状态序列。 |
| `project/cadvideo/pipeline/pose_align_viewer.py` | `cadscene_workbench/cadscene/core/coordinates.py` | web `cad_world` ↔ CAD meters ↔ python camera convention，特别是 pitch 反号。 |
| `project/cadvideo/pipeline/pose_align_viewer.py` | `cadscene_workbench/cadscene/cad/loader.py` | `CadBundle`、road center/edge/ref loading。 |
| `project/cadvideo/pipeline/pose_align_viewer.py` | `cadscene_workbench/cadscene/cad/projection.py` | CAD ground line projection、intrinsics、visible splitting。 |
| `project/cadvideo/pipeline/camera_pose_track_groundplane.py` | `cadscene_workbench/cadscene/cad/centerline.py` | `CenterlineModel`、station/lateral、camera xy / cad xy helper。 |
| `project/cadvideo/video_path/anchors.py` | `cadscene_workbench/cadscene/alignment/keyframes.py` | confirmed/manual keyframe filtering、anchor summary、source policy。 |
| `project/cadvideo/io_utils.py` | `cadscene_workbench/cadscene/core/io.py` | safe IO、utf-8-sig CSV helpers、Pathlib-first 文件读写。 |
| 无统一旧模块 | `cadscene_workbench/cadscene/core/artifacts.py` | 新增 ArtifactManager，统一 runs 输出与 manifest 登记。 |
| 无统一旧模块 | `cadscene_workbench/cadscene/core/manifest.py` | 新增 run manifest schema 与 stage record。 |

## CLI 映射

| 旧 CLI | 新 CLI |
|---|---|
| `project/cadvideo/pipeline/segment_video.py` | `cadscene_workbench/cadscene/cli/segment_video.py` |
| `project/cadvideo/pipeline/run_video_sfm.py` | `cadscene_workbench/cadscene/cli/run_sfm.py` |
| `project/cadvideo/pipeline/align_sfm_to_cad.py` | `cadscene_workbench/cadscene/cli/align_to_cad.py` |
| `project/cadvideo/pipeline/evaluate_sfm_alignment_quality.py` | `cadscene_workbench/cadscene/cli/evaluate_quality.py` |
| `project/cadvideo/pipeline/export_sfm_scene_for_viewer.py` | `cadscene_workbench/cadscene/cli/export_viewer_scene.py` |
| `project/cadvideo/pipeline/analyze_sfm_road_surface.py` | `cadscene_workbench/cadscene/cli/analyze_road_surface.py` |
| `project/cadvideo/pipeline/downsample_web_keyframes.py` | `cadscene_workbench/cadscene/cli/downsample_keyframes.py` |
| `project/cadvideo/pipeline/compare_keyframe_ablation.py` | `cadscene_workbench/cadscene/cli/compare_ablation.py` |
| `project/cadvideo/pipeline/serve_web_viewer.py` | `cadscene_workbench/cadscene/cli/serve_viewer.py` |
| 无统一旧 CLI | `cadscene_workbench/cadscene/cli/render_overlay.py` |
| 无统一旧 CLI | `cadscene_workbench/cadscene/cli/run_pipeline.py` |

## Viewer 映射

| 旧路径 | 新路径 | 说明 |
|---|---|---|
| `project/web_camera_viewer/index.html` | `cadscene_workbench/apps/web_camera_viewer/index.html` | 保留主入口，后续可再模块化。 |
| `project/web_camera_viewer/paths.js` | `cadscene_workbench/apps/web_camera_viewer/paths.js` | URL 参数和 dataset 默认路径解析需改为 runs-aware。 |
| `project/web_camera_viewer/fallback.js` | `cadscene_workbench/apps/web_camera_viewer/fallback.js` | 保留 fallback 能力。 |
| `project/web_camera_viewer/viewer_legacy.js` | `cadscene_workbench/apps/web_camera_viewer/viewer_legacy.js` | 保留 quality timeline、suggestions、SfM scene、双轨迹；后续再拆模块。 |
| `project/web_camera_viewer/vendor/` | `cadscene_workbench/apps/web_camera_viewer/vendor/` | 保留本地 Three.js，不依赖 CDN。 |

## Legacy 归档映射

| 旧路径 | 归档目标 |
|---|---|
| `project/cadvideo/pipeline/camera_pose_track*.py` | `cadscene_workbench/legacy/old_trackers/` |
| `project/cadvideo/pipeline/render_visible_window_overlay.py` | `cadscene_workbench/legacy/old_trackers/visible_window/` |
| `project/cadvideo/video_path/path_model.py`、`motion.py`、`cad_score.py`、`cad_refine.py`、`road_mask.py` | `cadscene_workbench/legacy/old_video_path/` |
| `project/cadvideo/pipeline/refine_semantic.py`、`project/cadvideo/sfm_align/semantic_refine.py` | `cadscene_workbench/legacy/deprecated_refine/semantic_refine/` |
| `project/cadvideo/pipeline/refine_video_camera_path_with_cad.py`、`visual_pose_refine.py` | `cadscene_workbench/legacy/deprecated_refine/cad_visual_refine/` |
| `project/cadvideo/pipeline/organize_hygs_outputs.py`、`hygs_prepare_realign.py`、`project/cadvideo/tools/organize_project_outputs.py` | `cadscene_workbench/legacy/experimental_scripts/cleanup_tools/` |

归档目录只允许文档或必要旧代码副本，不允许被 `cadscene/` 主线 import。

