# 新输出结构

新项目不再写旧 `out/`。所有运行产物统一写入 `runs/`，由 dataset 和 run_id 隔离。

## 目录布局

```text
runs/
  hygs_1min/
    <run_id>/
      00_inputs/
      01_segmentation/
      02_sfm/
      03_alignment/
      04_quality/
      05_viewer_scene/
      06_road_surface/
      07_keyframe_ablation/
      08_render/
      reports/
      logs/
      manifest.json
```

## 阶段职责

### 00_inputs

记录本次 run 使用的输入引用，不复制大文件。

建议内容：

- `dataset.yaml.resolved.json`
- `pipeline.yaml.resolved.json`
- 输入路径清单
- 关键 CLI override 记录

注意：

- 大视频、PLY、历史 track 不自动复制进 runs。
- 可以写相对路径和文件 hash；hash 计算失败不阻塞 dry-run。

### 01_segmentation

阶段：DINOv3 semantic segmentation。

产物：

- `masks/road/`
- `masks/static/`
- `preview/`
- `seg_manifest.json`
- `segmentation_report.md`

### 02_sfm

阶段：pycolmap / COLMAP SfM。

产物：

- `images/`
- `masks/`
- `database.db`
- `sparse/`
- `camera_trajectory.json`
- `sparse_points.ply`
- `camera_intrinsics.json`
- `sfm_stats.json`
- `sfm_report.md`

### 03_alignment

阶段：SfM → CAD global sim3 + segment anchoring。

产物：

- `alignment.json`
- `sfm_camera_path.csv`
- `camera_track_pred.json`
- `keyframe_correspondences.csv`
- `alignment_report.md`
- 可选 debug overlay sample

CSV 使用 `utf-8-sig`。

### 04_quality

阶段：质量评估 + 关键帧推荐。

产物：

- `quality_timeline.csv`
- `keyframe_suggestions.json`
- `quality_report.md`
- `quality_diagnostics.png`
- `camera_track_pred_quality.json`
- `suggestion_samples/`

CSV 使用 `utf-8-sig`，报告中文。

### 05_viewer_scene

阶段：viewer scene export。

产物：

- `sfm_viewer_scene.json`
- `sfm_viewer_scene_stats.json`
- `sfm_viewer_scene_report.md`

坐标规则：

- 点云只用 global sim3。
- `anchored_camera_path` 来自 `sfm_camera_path.csv`。
- 输出给前端使用 `cad_world` 表达。
- pitch 前端/后端取反必须集中在 `core/coordinates.py`。

### 06_road_surface

阶段：road surface / SfM geometry diagnostics。

产物：

- `sfm_geometry_report.md`
- `sfm_geometry_summary.json`
- `point_cloud_stats.json`
- `road_surface_profile.csv`
- `road_surface_diagnostics.png`
- `road_points_cadworld.ply`
- `keyframe_pose_residuals.csv`
- `keyframe_pose_residuals.png`
- `cad_flatness_error.csv`
- `camera_z_profile.csv`
- `camera_z_profile.png`
- `sfm_surface_quality.csv`
- 可选 `viewer_diagnostics_scene.json`

CSV 使用 `utf-8-sig`。

### 07_keyframe_ablation

阶段：keyframe density ablation。

产物：

- `camera_track_<interval>f_kf.json`
- `keyframe_downsample_report.md`
- `ablation_compare_report.md`
- `ablation_summary.json`

默认 demo 为 `240f`，但文档应标注：240f 是激进演示闭环，不是质量最优默认密度。

### 08_render

阶段：overlay render。

产物：

- `sfm_align_overlay.mp4`
- `render_report.md`
- `render_stats.json`

### reports

跨阶段报告：

- `run_summary.md`
- `acceptance_checklist.md`
- `known_issues.md`

### logs

日志：

- `run_pipeline.log`
- `stage_<name>.log`
- `commands.txt`

## manifest.json schema

每个阶段都必须登记到 `manifest.json`。

示例：

```json
{
  "schema_version": "1.0",
  "project_version": "v0.1-sfm-workbench",
  "dataset": "hygs_1min",
  "run_id": "demo_240f",
  "output_root": "runs",
  "created_at": "2026-07-07T00:00:00+08:00",
  "stages": [
    {
      "stage_name": "alignment",
      "command": [
        "python",
        "-m",
        "cadscene.cli.align_to_cad",
        "--dataset",
        "hygs_1min",
        "--run-id",
        "demo_240f"
      ],
      "inputs": {
        "trajectory": "runs/hygs_1min/demo_240f/02_sfm/camera_trajectory.json",
        "web_camera_track": "data/hygs_1min/camera_track_240f_kf.json",
        "cad_dir": "data/hygs_1min/cad"
      },
      "outputs": {
        "alignment": "runs/hygs_1min/demo_240f/03_alignment/alignment.json",
        "sfm_camera_path": "runs/hygs_1min/demo_240f/03_alignment/sfm_camera_path.csv"
      },
      "metrics": {
        "keyframe_count": 8,
        "sim3_scale": 2.6,
        "mean_keyframe_center_residual_m": 0.24
      },
      "status": "success",
      "created_at": "2026-07-07T00:00:00+08:00"
    }
  ]
}
```

状态枚举建议：

- `pending`
- `running`
- `success`
- `failed`
- `skipped`
- `dry_run`

## 兼容策略

- 旧 `out/` 不作为新输出目录。
- 第一版可以在 config 中引用旧输入路径作为 smoke 命令参数，但新 run 产物必须写入 `runs/`。
- 新项目删除 `project/` 后仍必须能读取 config 并运行合成数据测试。

