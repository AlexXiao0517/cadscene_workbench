# 新项目架构设计

新项目根目录后续为 `D:\zjic2026\cadscene_workbench\cadscene_workbench\`。本文只定义架构，不创建骨架、不迁移代码。

## 目录结构

```text
cadscene_workbench/
  pyproject.toml
  README.md
  docs/
  configs/
    datasets/
    pipelines/
  cadscene/
    core/
    cad/
    sfm/
    alignment/
    diagnostics/
    rendering/
    viewer/
    cli/
  apps/
    web_camera_viewer/
  tests/
  scripts/
  legacy/
    old_trackers/
    old_video_path/
    deprecated_refine/
    experimental_scripts/
  runs/
```

包名确定建议为 `cadscene`。不保留 `cadvideo` 主线 wrapper，避免新项目继续背旧命名和旧路径包袱。

## cadscene/core

职责：跨模块基础类型、坐标和安全 IO。

- `camera.py`
  - `CameraState`
  - yaw/pitch/roll/FOV 基础表达
  - camera path row ↔ typed state
- `coordinates.py`
  - web `cad_world` ↔ CAD meters
  - python pitch 与前端 pitch 反号规则
  - `cad_scale` / `origin_xy` 集中转换
- `sim3.py`
  - global sim3 apply / inverse
  - rotation transform
  - JSON schema load/save
- `artifacts.py`
  - `ArtifactManager`
  - stage 输出目录解析
  - safe write JSON / CSV / Markdown / images / video path registration
- `manifest.py`
  - `manifest.json` schema
  - stage record：`stage_name`、`command`、`inputs`、`outputs`、`metrics`、`status`、`created_at`
- `io.py`
  - Pathlib-first safe IO
  - CSV 默认 `utf-8-sig`
  - JSON/Markdown 默认 UTF-8

## cadscene/cad

职责：CAD 数据加载、道路中心线与投影。

- `loader.py`
  - road center/edge/ref 读取
  - `CadBundle`
  - design JSON / road JSON 兼容
- `centerline.py`
  - `CenterlineModel`
  - station/lateral projection
  - station/lateral → CAD xy
- `projection.py`
  - CAD ground points projection
  - intrinsics
  - visible polyline splitting
- `coordinates.py`
  - CAD raw coordinates 与 CAD meters 之间转换

## cadscene/sfm

职责：语义分割、SfM 重建、轨迹和点云。

- `segmentation.py`
  - DINOv3 segmentation
  - road/static mask
  - preview / manifest data
  - torch/transformers 惰性导入
- `reconstruction.py`
  - pycolmap / COLMAP orchestration
  - frame extraction
  - mask preparation
  - reconstruction stats
- `trajectory.py`
  - `camera_trajectory.json` parsing
  - frame query/interpolation
  - intrinsics / horizontal FOV
- `pointcloud.py`
  - PLY load/write
  - point sampling
  - sim3 application

## cadscene/alignment

职责：SfM → CAD 对齐、关键帧、质量评估、ablation。

- `keyframes.py`
  - web camera track load
  - confirmed/manual 过滤
  - `algorithm_prediction` 不作为人工锚点
  - anchor summary
- `aligner.py`
  - correspondence build
  - global sim3 estimate
  - segment anchoring residual interpolation
  - aligned camera path generation
- `quality.py`
  - anchor distance / segment drift / correction / sfm quality / visual / turn motion risk
  - adaptive percentile threshold
  - keyframe suggestions
- `keyframes_downsample.py`
  - 120f / 180f / 240f 降采样
  - 中文报告
- `ablation.py`
  - baseline vs sparse keyframe metrics
  - 对比报告

## cadscene/diagnostics

职责：只读诊断，不修改轨迹或 CAD。

- `road_surface.py`
  - road corridor extraction
  - z=0 / global tilted plane / station profile diagnostics
  - reliability and recommendation
- `geometry.py`
  - point cloud stats
  - bbox overlap
  - station quality score
- `pose_residual.py`
  - manual vs global SfM / anchored pose residual
  - camera z profile
  - pose compensation warning

## cadscene/rendering

职责：overlay render 和样式。

- `overlay.py`
  - faded overlay video
  - frame rendering
  - output video writing
- `styles.py`
  - line colors
  - alpha / linewidth / fade policy

## cadscene/viewer

职责：后端导出 viewer 可读数据。

- `export_scene.py`
  - `sfm_viewer_scene.json`
  - global_sfm_track
  - anchored_camera_path
  - suggestions
  - point cloud schema
- `schema.py`
  - viewer JSON schema 常量和验证

## cadscene/cli

职责：薄 CLI，只做配置解析、参数覆盖、ArtifactManager stage 编排，不放复杂算法。

必须提供：

- `segment_video.py`
- `run_sfm.py`
- `align_to_cad.py`
- `evaluate_quality.py`
- `export_viewer_scene.py`
- `analyze_road_surface.py`
- `downsample_keyframes.py`
- `compare_ablation.py`
- `render_overlay.py`
- `serve_viewer.py`
- `run_pipeline.py`

## apps/web_camera_viewer

从 `project/web_camera_viewer/` 迁入。第一版可以保留 legacy script 结构，先保证功能等价：

- URL 参数：`dataset`、`video`、`cad`、`track`、`suggestions`、`qualityTimeline`、`sfmScene`
- quality timeline 风险色带
- suggestions 标记和点击跳帧
- SfM sparse point cloud
- global SfM track 与 anchored path 双轨迹
- 当前加载 track 覆盖 3D anchored path
- `algorithm_prediction` 不作为人工关键帧
- HTTP Range 服务由 `cadscene.cli.serve_viewer` 提供

## legacy

旧方法只归档，不进入主线 import：

- `old_trackers/`
- `old_video_path/`
- `deprecated_refine/`
- `experimental_scripts/`

每个目录必须有 README，说明方法名称、为什么废弃、替代主线、是否仍可参考、不建议继续使用。

