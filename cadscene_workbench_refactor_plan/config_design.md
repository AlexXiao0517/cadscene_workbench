# 配置系统设计

配置分两类：dataset config 描述输入数据和 CAD 资产；pipeline config 描述要跑哪些阶段、阶段参数和输出子目录。CLI override 优先级高于 config，但所有 override 必须写入 manifest。

## datasets/hygs_1min.yaml

路径：

```text
cadscene_workbench/configs/datasets/hygs_1min.yaml
```

建议字段：

```yaml
dataset_name: hygs_1min
version: v0.1-sfm-workbench

video_path: data/hygs_1min/hygs_1min.mp4
cad_dir: data/hygs_1min/cad
cad_scale: 0.06
origin_xy:
  - 0.0
  - 0.0

default_track: data/hygs_1min/camera_track_240f_kf.json
trajectory_path: null
sparse_ply_path: null

fps: 25.0
frame_range:
  start_frame: 0
  end_frame: 1500

viewer:
  default_video: data/hygs_1min/hygs_1min.mp4
  default_cad: data/hygs_1min/design.json
  default_track: data/hygs_1min/camera_track_240f_kf.json
```

说明：

- `trajectory_path` 和 `sparse_ply_path` 可为空，表示由当前 run 的 `02_sfm/` 产物提供。
- 如果 smoke 命令要消费已有真实产物，可以通过 CLI override 指定路径，不要求智能体完整跑真实大视频。
- 不写死 `out/hygs`；旧路径只能作为人工 smoke 命令参考。

## pipelines/sfm_overlay.yaml

路径：

```text
cadscene_workbench/configs/pipelines/sfm_overlay.yaml
```

建议字段：

```yaml
pipeline_name: sfm_overlay
version: v0.1-sfm-workbench

stages:
  segmentation:
    enabled: true
    output_subdir: 01_segmentation
    inputs:
      video_path: "${dataset.video_path}"
    params:
      start_frame: 0
      num_frames: 0
      frame_step: 5
      model_id: nielsr/eomt-dinov3-ade-semantic-large-512
      device: null
      preview_every: 25
      preview_scale: 0.5
      exclude_vegetation: false
      keep_water: false

  sfm:
    enabled: true
    output_subdir: 02_sfm
    inputs:
      video_path: "${dataset.video_path}"
      seg_dir: "${run.01_segmentation}"
    params:
      frame_step: 5
      max_image_size: 2048
      max_num_features: 12000
      camera_model: OPENCV
      sequential_overlap: 15
      init_min_tri_angle: 2.0
      min_reg_images: 10
      use_mask: true

  alignment:
    enabled: true
    output_subdir: 03_alignment
    inputs:
      trajectory: "${run.02_sfm.camera_trajectory}"
      web_camera_track: "${dataset.default_track}"
      cad_dir: "${dataset.cad_dir}"
    params:
      cad_scale: "${dataset.cad_scale}"
      origin_xy: "${dataset.origin_xy}"
      frame_step: 1
      fov: null
      fov_from: sfm
      frontend_track_step: 60

  quality:
    enabled: true
    output_subdir: 04_quality
    inputs:
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      alignment: "${run.03_alignment.alignment}"
      web_camera_track: "${dataset.default_track}"
      trajectory: "${run.02_sfm.camera_trajectory}"
      seg_dir: "${run.01_segmentation}"
    params:
      max_suggestions: 10
      min_suggestion_gap: 40
      suggestion_risk_threshold: 0.65
      adaptive_percentile: 85.0
      no_suggestion_samples: true

  viewer_scene:
    enabled: true
    output_subdir: 05_viewer_scene
    inputs:
      sparse_ply: "${run.02_sfm.sparse_points}"
      trajectory: "${run.02_sfm.camera_trajectory}"
      alignment: "${run.03_alignment.alignment}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      quality_timeline: "${run.04_quality.quality_timeline}"
      suggestions: "${run.04_quality.keyframe_suggestions}"
    params:
      max_points: 80000
      point_sample_mode: voxel
      voxel_size: 0.2
      include_rgb: true
      include_track_global: true
      include_track_anchored: true
      include_suggestions: true

  road_surface:
    enabled: true
    output_subdir: 06_road_surface
    inputs:
      sparse_ply: "${run.02_sfm.sparse_points}"
      trajectory: "${run.02_sfm.camera_trajectory}"
      alignment: "${run.03_alignment.alignment}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      web_camera_track: "${dataset.default_track}"
      cad_dir: "${dataset.cad_dir}"
    params:
      max_points: 20000
      point_sample_mode: voxel
      voxel_size: 0.5
      station_bin_m: 20.0
      road_corridor_width: 15.0
      export_viewer_scene: true

  keyframe_ablation:
    enabled: true
    output_subdir: 07_keyframe_ablation
    inputs:
      baseline_track: data/hygs_1min/camera_track_26kf.json
    params:
      intervals:
        - 120
        - 180
        - 240
      demo_interval: 240

  render:
    enabled: true
    output_subdir: 08_render
    inputs:
      video_path: "${dataset.video_path}"
      cad_dir: "${dataset.cad_dir}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
    params:
      debug_scale: 1.0
      overlay_linewidth: 3
      overlay_alpha: 0.88
      faded_overlay: true
      max_distance_m: 900.0
      fade_start_m: 250.0
```

## 完整流程目标命令

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay.yaml \
  --run-id demo_240f \
  --output-root runs
```

## 参数优先级

从低到高：

1. 代码默认值
2. dataset config
3. pipeline config
4. CLI override

## 配置校验

第一版至少校验：

- `dataset_name` 非空。
- `cad_scale > 0`。
- `origin_xy` 是长度为 2 的数值列表。
- enabled stage 的 required inputs 能解析。
- `output_subdir` 不包含 `..`，且只指向当前 run 目录下。
- CSV 输出统一由 `core/io.py` 使用 `utf-8-sig`。

