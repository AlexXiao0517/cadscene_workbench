# 新 CLI 设计

新 CLI 统一放在 `cadscene_workbench/cadscene/cli/`。所有 CLI 都必须是薄入口：只解析配置、处理 CLI override、创建 ArtifactManager stage、调用核心模块、登记 manifest。复杂算法不得写在 CLI 中。

## 统一参数

所有 CLI 支持：

```bash
--config configs/pipelines/xxx.yaml
--dataset hygs_1min
--run-id 20260707_test
--output-root runs
```

必要覆盖参数保留，但覆盖值必须进入 stage manifest 的 `command` 或 `params` 记录。

通用约定：

- `--dataset` 解析 `configs/datasets/<dataset>.yaml`。
- `--config` 解析 pipeline 或 stage config。
- `--run-id` 决定 `runs/<dataset>/<run_id>/`。
- `--output-root` 默认 `runs`。
- CSV 输出使用 `utf-8-sig`。
- 报告输出使用中文 Markdown。
- 路径统一使用 Pathlib。

## 命令列表

### segment_video

```bash
python -m cadscene.cli.segment_video \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 读取 dataset `video_path`。
- 运行 DINOv3 segmentation。
- 输出到 `01_segmentation/`。
- 登记 masks、preview、seg manifest。

覆盖参数：

- `--video`
- `--start-frame`
- `--num-frames`
- `--frame-step`
- `--model-id`
- `--device`
- `--preview-every`
- `--preview-scale`
- `--exclude-vegetation`
- `--keep-water`

### run_sfm

```bash
python -m cadscene.cli.run_sfm \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 消费 `01_segmentation/` masks。
- 运行 pycolmap/COLMAP。
- 输出 `camera_trajectory.json`、`sparse_points.ply`、intrinsics、SfM stats 到 `02_sfm/`。

覆盖参数：

- `--video`
- `--seg-dir`
- `--start-frame`
- `--num-frames`
- `--frame-step`
- `--max-image-size`
- `--max-num-features`
- `--camera-model`
- `--sequential-overlap`
- `--init-min-tri-angle`
- `--reuse-database`
- `--export-only`
- `--colmap-exe`
- `--use-mask/--no-mask`

### align_to_cad

```bash
python -m cadscene.cli.align_to_cad \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs \
  --web-camera-track data/hygs_1min/camera_track_240f_kf.json
```

职责：

- 读取 `02_sfm/camera_trajectory.json`。
- 读取人工 keyframe track。
- 读取 CAD 配置。
- 估计 global sim3，生成 anchored camera path。
- 输出到 `03_alignment/`。

覆盖参数：

- `--trajectory`
- `--web-camera-track`
- `--cad-dir`
- `--cad-scale`
- `--origin-xy`
- `--start-frame`
- `--end-frame`
- `--frame-step`
- `--fov`
- `--fov-from`
- `--frontend-track-step`
- `--no-frontend-track`

### evaluate_quality

```bash
python -m cadscene.cli.evaluate_quality \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 消费 `03_alignment/sfm_camera_path.csv` 和 `alignment.json`。
- 生成 `quality_timeline.csv`、`keyframe_suggestions.json`、中文报告。
- 输出到 `04_quality/`。

覆盖参数：

- `--web-camera-track`
- `--trajectory`
- `--seg-dir`
- `--video`
- `--fps`
- `--max-suggestions`
- `--min-suggestion-gap`
- `--suggestion-risk-threshold`
- `--no-suggestion-samples`
- 风险权重和阈值参数。

### export_viewer_scene

```bash
python -m cadscene.cli.export_viewer_scene \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 消费 sparse PLY、trajectory、alignment、quality。
- 输出 `sfm_viewer_scene.json`、stats、report 到 `05_viewer_scene/`。

覆盖参数：

- `--sparse-ply`
- `--trajectory`
- `--alignment`
- `--sfm-camera-path`
- `--quality-timeline`
- `--suggestions`
- `--max-points`
- `--point-sample-mode`
- `--voxel-size`
- `--include-rgb/--no-include-rgb`
- `--no-points`
- `--no-tracks`

### analyze_road_surface

```bash
python -m cadscene.cli.analyze_road_surface \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 只读诊断 SfM geometry 和 road surface。
- 输出中文 geometry report、CSV、PNG、diagnostics scene 到 `06_road_surface/`。

覆盖参数：

- `--sparse-ply`
- `--trajectory`
- `--alignment`
- `--sfm-camera-path`
- `--web-camera-track`
- `--cad-dir`
- `--cad-scale`
- `--seg-dir`
- `--max-points`
- `--point-sample-mode`
- `--voxel-size`
- `--station-bin-m`
- `--road-corridor-width`
- `--export-viewer-scene`

### downsample_keyframes

```bash
python -m cadscene.cli.downsample_keyframes \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs \
  --input data/hygs_1min/camera_track_26kf.json \
  --interval 240
```

职责：

- 只保留 confirmed/manual keyframes。
- 跳过 `algorithm_prediction`。
- 输出稀疏 track 和中文报告到 `07_keyframe_ablation/`。

覆盖参数：

- `--input`
- `--output`
- `--interval`

### compare_ablation

```bash
python -m cadscene.cli.compare_ablation \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 对比 baseline 和 sparse keyframe run 的 alignment / quality 指标。
- 输出中文对比报告到 `07_keyframe_ablation/`。

### render_overlay

```bash
python -m cadscene.cli.render_overlay \
  --config configs/pipelines/sfm_overlay.yaml \
  --dataset hygs_1min \
  --run-id demo_240f \
  --output-root runs \
  --faded-overlay
```

职责：

- 消费 aligned camera path。
- 输出 overlay mp4 到 `08_render/`。

覆盖参数：

- `--video`
- `--cad-dir`
- `--sfm-camera-path`
- `--debug-scale`
- `--overlay-linewidth`
- `--overlay-alpha`
- `--faded-overlay`
- `--max-distance-m`
- `--fade-start-m`

### serve_viewer

```bash
python -m cadscene.cli.serve_viewer \
  --bind 127.0.0.1 \
  --port 8300
```

职责：

- 从新项目根目录提供支持 HTTP Range 的静态服务。
- 服务 `apps/web_camera_viewer/`、`runs/`、必要 dataset assets。

### run_pipeline

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay.yaml \
  --run-id demo_240f \
  --output-root runs
```

职责：

- 按 pipeline config 顺序执行 enabled stages。
- 支持 `--dry-run` 输出将执行命令和 artifact 路径，但不运行重任务。
- 每个 stage 都写入 `manifest.json`。

