# run_pipeline 使用说明

`run_pipeline` 是面向维护和批处理的命令行入口；日常项目操作从工作流门户开始：

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

门户的正式端到端路径包括 `sfm_only`（**Stable**）、固定相机中心的
`pure_rotation`（**Supported**）以及带显式确认门槛的 `srt_full_pose` 和
`srt_fixed_track_visual_pose`。自动视频分析与路线推荐仍需人工复核；历史
`srt_sfm_fused` 只读兼容。SRT 检测标签本身不能替代坐标、FOV、高度和结果验证。

正式上传界面当前只接受 MP4、DXF 和可选 SRT。上传页不要求选择运动模式；分析会自动
检测运动特征并保守推荐 `sfm_only` 或 `pure_rotation`，项目片段管理页再显示最终工作流
并允许人工覆盖。下文的 `run_pipeline`、`data/` 和 `runs/` 是维护/兼容入口，不代表
正式项目界面支持额外文件格式。

当前提供四类主要 pipeline：

- `sfm_overlay_existing_sfm.yaml`：消费已有 `camera_trajectory.json` 和
  `sparse_points.ply`。
- `sfm_overlay_with_sfm.yaml`：从视频运行 SfM，再串联 alignment、quality、
  viewer_scene、road_surface 和 render。
- `srt_full_pose_overlay.yaml`：消费 `02_srt_full_pose/camera_trajectory_full_pose.json`，
  以 `scale=1.0` 的 metric-direct 模式运行 alignment/viewer/render；不接收 sparse PLY，
  不运行 SfM 和 road-surface。
- `srt_fixed_track_visual_pose_overlay.yaml`：消费
  `02_srt_visual_pose/camera_trajectory_visual_pose.json`，只运行 `alignment,render`；
  不接收 sparse PLY，也没有 SfM、quality、viewer_scene 或 road-surface 阶段。

这里列的是“消费已生成轨迹”的兼容 YAML 阶段，不是 Project API 的轨迹构建步骤。
正式 `srt_fixed_track_visual_pose` 项目在产生上述轨迹之前会自适应抽帧运行 COLMAP 稀疏
重建、三角化/BA 与 RADIAL 标定，并保留可选诊断点云；不能据此处 YAML 的阶段列表推断
该项目路线不运行 SfM。`srt_full_pose` 才是直接读取完整 SRT 姿态并跳过 SfM 的分支。

## 准备输入

完整流程需要原始视频、web viewer keyframe track、CAD assets、`cad_scale` 和
`origin_xy`。SfM 阶段默认无 mask；DINOv3 segmentation 尚未迁移。

人工关键帧应包含经确认的相机视场角（FOV）。当重建内参或几何不可靠时，彼此一致的
已确认人工关键帧 FOV 优先于重建 FOV；不要把 SfM 或普通 SRT 元数据宣传为必然准确的
FOV 来源。全姿态管线的 FOV 是用户输入的单一水平角度，随轨迹内参传递。

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

服务的 `--root` 是静态站点根；`--storage-root` 是 workflow 的 `data/` 与 `runs/`
根，默认继承 `--root`。部署时若把两者分开，先建立存储目录，再显式指定：

```powershell
python -m cadscene.cli.serve_viewer `
  --root . `
  --storage-root D:\cadscene-storage `
  --bind 127.0.0.1 --port 8300
```

分离后浏览器仍通过 `/data/` 和 `/runs/` 读取存储根内容；不要用 `--extra-root`
代替工作流存储根。

## 引导式关键帧与质量

门户中的标准顺序是：先保存至少两个已确认人工关键帧并完成初步路线拟合；再生成关键
帧计划，逐项完成计划帧的人工标定，最后重新路线拟合并运行质量检测。计划中的待标定帧
不会自动计入人工锚点。质量检测会校验计划已完成且其后已重新路线拟合，因此不能跳过
这两个条件直接运行 quality。

`road_surface` 是 quality 路径中的可选诊断阶段。当 CAD 没有可用道路中心线时，运行
manifest 会将它标为 `skipped`；这不是对齐、质量或渲染成功的替代证明。可用时，诊断
场景写入 `06_road_surface/viewer_diagnostics_scene.json`。

## 常见错误

- 缺少视频：检查 dataset config 的 `video_path` 或 `--video`。
- 缺少 pycolmap：切换到 difusser 环境并安装 `cadscene-workbench[sfm]`。
- CAD 对齐异常：核对 `cad_scale` 和 `origin_xy`。
- FOV 与画面对不上：优先检查人工关键帧中已确认且一致的 FOV；若重建 FOV 不可靠，
  使用人工值复核，不要用普通 SRT 修正。
- viewer URL 无法打开：必须通过 `serve_viewer`，不要使用 `file://`。
- 质量检测被拒绝：先完成关键帧计划，再重新路线拟合。
- 道路诊断显示跳过：检查 CAD 是否有可用道路中心线；其余质量/渲染结果应单独判断。
- render 失败：检查视频编码和 OpenCV MP4 writer 支持。

## 当前限制

- 不包含 segmentation / DINOv3；
- 不做 semantic refine；
- 不做 CAD-on-tilted-plane apply；
- 真实 hygs SfM 仅作为人工 smoke，不进入单元测试。
