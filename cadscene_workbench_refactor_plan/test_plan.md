# 测试计划

测试目标：核心模块均有测试；能用合成数据测试的，不依赖真实大视频；真实 hygs 数据只作为 smoke 命令，不要求智能体完整运行。

## 单元测试

### Coordinate conversion

覆盖：

- web `cad_world` → CAD meters：`x_m = (x - origin_x) * cad_scale`，`y_m = (y - origin_y) * cad_scale`，`z_m = z * cad_scale`。
- CAD meters → web `cad_world`：反向转换。
- `origin_xy` 和 `cad_scale` 必须集中由 `core/coordinates.py` 处理。

建议测试：

- `tests/core/test_coordinates.py::test_web_camera_to_cad_meters`
- `tests/core/test_coordinates.py::test_cad_meters_to_web_camera_roundtrip`

### Pitch sign

覆盖：

- 前端 pitch 与 python CameraState pitch 反号。
- viewer scene 导出 anchored track 时 pitch 必须反号。
- road diagnostics 读取 web keyframe 时 pitch 必须反号。

建议测试：

- `tests/core/test_coordinates.py::test_pitch_sign_web_python_conversion`
- `tests/viewer/test_export_scene.py::test_load_anchored_track_pitch_negated`

### Sim3 apply/inverse

覆盖：

- `Sim3.apply(points)`
- `Sim3.inverse().apply(Sim3.apply(points)) == points`
- rotation transform 不改变旋转矩阵正交性。
- JSON load/save 后数值一致。

建议测试：

- `tests/core/test_sim3.py`

### Keyframe confirmed/manual 过滤

覆盖：

- `manual_keyframe`、`confirmed_keyframe`、`manual_*` 保留。
- `algorithm_prediction` 不作为人工锚点。
- 缺少 source 时默认策略需明确；建议默认视为 manual_keyframe 以兼容旧 track，但写入 warning。

建议测试：

- `tests/alignment/test_keyframes.py::test_confirmed_keyframes_skip_algorithm_prediction`
- `tests/alignment/test_keyframes.py::test_algorithm_prediction_not_used_as_anchor`

### Alignment smoke

用合成 trajectory 和 2-3 个关键帧：

- global sim3 能恢复已知 scale/rotation/translation。
- anchored path 精确穿过人工 keyframe。
- 首末关键帧之外保持恒定 residual。

建议测试：

- `tests/alignment/test_aligner.py::test_estimate_similarity_recovers_known_transform`
- `tests/alignment/test_aligner.py::test_anchored_align_passes_keyframes`

### Quality bootstrap / QA

覆盖旧 `tests/test_sfm_alignment_quality.py` 中核心行为：

- risk weights normalization。
- 缺失组件自动重归一。
- all unavailable 返回空权重或明确 fallback。
- risk level 阈值。
- suggestions max count、min gap、risk threshold。
- CLI dry-run / synthetic input 产出 `quality_timeline.csv`、`keyframe_suggestions.json`、`quality_report.md`。
- CSV BOM：`utf-8-sig`。

建议测试：

- `tests/alignment/test_quality.py`
- `tests/cli/test_evaluate_quality_cli.py`

### Viewer scene schema

覆盖：

- 点云采样不超过 max points。
- voxel/random/uniform 采样行为。
- CAD meters → frontend cad_world。
- no points / no tracks / no suggestions 可运行。
- schema 包含 `point_cloud`、`global_sfm_track`、`anchored_camera_path`、`suggestions`、`created_at`。
- 点云只用 global sim3，不套 anchored correction。

建议测试：

- `tests/viewer/test_export_scene.py`
- `tests/viewer/test_schema.py`

### Road surface diagnostics

覆盖旧 `tests/test_sfm_road_surface_diagnostics.py` 的合成数据能力：

- station binning。
- signed lateral。
- z=0 flat plane error。
- global plane fit。
- station profile residual。
- road corridor extraction。
- insufficient points classification。
- `pose_compensation_warning`。
- CLI smoke with synthetic PLY/trajectory/alignment/CAD。

建议测试：

- `tests/diagnostics/test_road_surface.py`
- `tests/diagnostics/test_pose_residual.py`
- `tests/cli/test_analyze_road_surface_cli.py`

### Keyframe downsample

覆盖：

- target frames 保留末帧。
- nearest tie 选择较小 frame。
- 跳过 `algorithm_prediction`。
- 26kf → 120f/180f/240f 的数量和首末帧规则。
- 中文报告生成。

建议测试：

- `tests/alignment/test_keyframe_downsample.py`

### Render overlay smoke

使用合成小视频或 2-3 张帧：

- `render_overlay` 可以生成非空 mp4 或 dry-run manifest。
- faded overlay 参数进入 manifest。
- 无 CAD line 时给出可解释失败或空 overlay 状态。

建议测试：

- `tests/rendering/test_overlay_smoke.py`
- `tests/cli/test_render_overlay_cli.py`

### CLI --help

所有 CLI 必须支持 `--help`：

- `segment_video`
- `run_sfm`
- `align_to_cad`
- `evaluate_quality`
- `export_viewer_scene`
- `analyze_road_surface`
- `downsample_keyframes`
- `compare_ablation`
- `render_overlay`
- `serve_viewer`
- `run_pipeline`

建议测试：

- `tests/cli/test_help.py`

### Artifact manifest

覆盖：

- stage record 包含 `stage_name`、`command`、`inputs`、`outputs`、`metrics`、`status`、`created_at`。
- failed/skipped/dry_run 状态可写。
- `output_subdir` 不能逃逸 run root。
- CSV helper 使用 `utf-8-sig`。

建议测试：

- `tests/core/test_artifacts.py`
- `tests/core/test_manifest.py`

### run_pipeline dry-run

覆盖：

- dry-run 不执行重任务。
- dry-run 仍解析 dataset/pipeline config。
- dry-run 输出将创建的 stage 目录和命令。
- manifest status 为 `dry_run`。

建议测试：

- `tests/cli/test_run_pipeline_dry_run.py`

## 独立性检查

新增脚本：

```text
cadscene_workbench/scripts/check_no_project_dependency.py
```

扫描 `cadscene_workbench/`，发现以下内容即失败：

- `import project`
- `from project`
- `project/`
- `../project`
- `cadvideo.sfm_align`
- `from cadvideo`
- `import cadvideo`

注意：`legacy/README.md` 可以提到旧路径作为说明，但主线代码、configs、tests、docs 中不得出现运行依赖。脚本需要支持白名单注释或只对白名单文档放宽。

建议测试：

- `tests/scripts/test_check_no_project_dependency.py`

## 真实数据 smoke 命令

真实 hygs 数据不要求智能体完整跑。文档中保留人工运行命令模板，例如：

```bash
python -m cadscene.cli.run_pipeline \
  --dataset hygs_1min \
  --config configs/pipelines/sfm_overlay.yaml \
  --run-id demo_240f \
  --output-root runs
```

若需要复用已有真实产物：

```bash
python -m cadscene.cli.analyze_road_surface \
  --dataset hygs_1min \
  --run-id smoke_existing \
  --output-root runs \
  --sparse-ply <existing_sparse_points.ply> \
  --trajectory <existing_camera_trajectory.json> \
  --alignment <existing_alignment.json> \
  --sfm-camera-path <existing_sfm_camera_path.csv> \
  --web-camera-track <existing_camera_track.json>
```

