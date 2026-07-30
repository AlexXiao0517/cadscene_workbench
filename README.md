# cadscene_workbench

## Stable baseline: v0.1.0-road-sfm-only

This release freezes the stable no-SRT CAD video alignment workflow. It supports local video and CAD import, pycolmap/COLMAP SfM, manual keyframe calibration, SfM-to-CAD alignment, quality checks, viewer-scene and road-surface diagnostics, and rendered-video preview/download through the frontend workflow.

### Supported no-SRT workflow

Run SfM from a local video, provide the CAD and manual keyframe calibration inputs, then run the configured pipeline to produce alignment, quality, viewer-scene, road diagnostics, and render outputs. No SRT input is required for this version.

Start the local viewer with:

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

User-owned datasets and generated artifacts stay outside version control. For example:

```text
data/<dataset>/input.mp4
data/<dataset>/cad/
runs/<dataset>/<run_id>/
```

`data/` and `runs/` are intentionally excluded from Git, along with videos, SRT, CAD source files, point clouds, COLMAP databases, and model weights.

### Current boundaries

- Video and CAD are required inputs; SRT telemetry is optional.
- `sfm_only` is the stable, genuinely runnable no-SRT workflow (`ready`).
- Upload analysis conservatively routes optional SRT as `srt_sfm_fused` or
  `srt_full_pose` only when its metadata coverage is sufficient. Both are
  currently `interface_only`: the UI can show their analysis, but fusion and
  direct full-pose execution are not implemented.
- SRT detection is not a high-precision trajectory or pose truth claim; retain
  SfM, manual calibration, SfM-to-CAD alignment, and quality checks.
- CUDA acceleration is not part of this baseline.
- Optional pycolmap and other runtime dependencies are not bundled or introduced by this release.

See [workflow routing](docs/workflow_routing.md) and [SRT capability
detection](docs/srt_capability_detection.md) for inputs, modes, and limits.

### Roadmap

See [docs/roadmap.md](docs/roadmap.md): CUDA COLMAP backend in v0.2.0,
upload analysis and Stage 6A routing in v0.3.0, partial-SRT execution in
v0.4.0, and a full-pose SRT execution workflow in v0.5.0.

`cadscene_workbench` 是独立的 SfM-CAD 视频配准工作台，Python 包名为 `cadscene`。
所有运行产物写入 `runs/<dataset>/<run_id>/`，报告默认中文，CSV 统一使用
`utf-8-sig`。`project/` 仅为只读迁移参考，新项目不依赖其中任何模块或路径。

## v0.1-sfm-workbench

当前稳定流程包括：

- 从原始视频运行 pycolmap SfM，或消费已有 SfM 产物；
- SfM 到 CAD 的 global sim3 与分段关键帧锚定；
- quality 时间线和建议帧；
- viewer scene、道路表面只读诊断和 overlay render；
- legacy web viewer 与 `runs/` 输出联动。

240f keyframe ablation 是激进闭环演示，不代表推荐默认关键帧密度。

## 从视频运行 SfM

真实 SfM 是重任务，请在安装了 `pycolmap` 和 OpenCV 的 difusser 环境运行：

```bash
python -m cadscene.cli.run_sfm \
  --dataset hygs_1min \
  --run-id sfm_smoke_0_250_s5 \
  --output-root runs \
  --video data/hygs_1min/hygs_1min.mp4 \
  --start-frame 0 \
  --num-frames 251 \
  --frame-step 5 \
  --init-min-tri-angle 2 \
  --no-mask
```

产物位于 `runs/hygs_1min/sfm_smoke_0_250_s5/02_sfm/`。

## 一键流程

从原始视频开始：

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay_with_sfm.yaml \
  --run-id demo_with_sfm \
  --output-root runs \
  --video data/hygs_1min/hygs_1min.mp4 \
  --web-camera-track data/hygs_1min/camera_track_240f_kf.json \
  --cad-dir data/hygs_1min/cad \
  --cad-scale 0.06 \
  --origin-xy 567747.5756295 3330464.2234675
```

消费已有 SfM 产物时使用
`configs/pipelines/sfm_overlay_existing_sfm.yaml`，并传入 `--trajectory` 与
`--sparse-ply`。运行前可追加 `--dry-run` 检查解析后的输入、输出和命令。

无道路中心线总平面场景与多分辨率视频规则见 `docs/stage5b_site_plan_compatibility.md`。

## Web viewer

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

打开：

```text
http://127.0.0.1:8300/apps/web_camera_viewer/?dataset=<dataset>&runId=<run_id>
```

更多说明见 `docs/pipeline_usage.md` 和 `docs/web_viewer_usage.md`。

## 当前限制

- DINOv3 segmentation 尚未迁移，SfM 默认使用 `--no-mask`；
- 不做 semantic refine；
- 不做 CAD-on-tilted-plane apply；
- `pycolmap` 为惰性可选依赖，普通 import 和 CLI `--help` 不会加载它。
