# run_pipeline 使用说明

`v0.1-sfm-workbench` 提供两条 pipeline：

- `sfm_overlay_existing_sfm.yaml`：消费已有 `camera_trajectory.json` 和
  `sparse_points.ply`。
- `sfm_overlay_with_sfm.yaml`：从视频运行 SfM，再串联 alignment、quality、
  viewer_scene、road_surface 和 render。

## 准备输入

完整流程需要原始视频、web viewer keyframe track、CAD assets、`cad_scale` 和
`origin_xy`。SfM 阶段默认无 mask；DINOv3 segmentation 尚未迁移。

## dry-run

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay_with_sfm.yaml \
  --run-id demo_with_sfm \
  --video data/hygs_1min/hygs_1min.mp4 \
  --dry-run
```

dry-run 会生成 `00_inputs/`、`logs/commands.txt`、
`reports/run_summary.md`，并在 manifest 中登记 `dry_run`，但不会运行 SfM。

## 完整流程

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

只跑 SfM：

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay_with_sfm.yaml \
  --run-id sfm_only \
  --stages sfm
```

也可使用 `--stages sfm,alignment,quality`，或使用
`--skip-render`、`--skip-road-surface`、`--skip-viewer-scene`。

## 直接运行 run_sfm

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

如果执行环境没有 pycolmap，真正运行时会给出明确提示；`--help` 和项目普通
import 不受影响。

## 输出与 viewer

SfM 输出位于 `02_sfm/`，包括轨迹、稀疏点云、内参、统计和中文报告。完整
pipeline 结束后查看 `reports/viewer_url.txt`，并先启动：

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

## 常见错误

- 缺少视频：检查 dataset config 的 `video_path` 或 `--video`。
- 缺少 pycolmap：切换到 difusser 环境并安装 `cadscene-workbench[sfm]`。
- CAD 对齐异常：核对 `cad_scale` 和 `origin_xy`。
- viewer URL 无法打开：必须通过 `serve_viewer`，不要使用 `file://`。
- render 失败：检查视频编码和 OpenCV MP4 writer 支持。

## 当前限制

- 不包含 segmentation / DINOv3；
- 不做 semantic refine；
- 不做 CAD-on-tilted-plane apply；
- 真实 hygs SfM 仅作为人工 smoke，不进入单元测试。
