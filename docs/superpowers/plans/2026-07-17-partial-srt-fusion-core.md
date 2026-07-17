# Stage 6B-1 Partial-SRT 融合核心实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 为 partial-SRT 提供可拒绝退化输入的 SfM→本地 ENU 融合 CLI，并输出可由既有 `align_to_cad` 读取的融合 trajectory。

**架构：** 复用 Stage 6A SRT parser，将 SfM 注册帧映射到以 PTS 优先的 SRT 时间轴；将经纬度转换为水平 ENU 和独立相对 Up。标准欧氏 Sim3 将 SfM 映射至 ENU，RANSAC 只用加权残差评分；融合位置由 metric SfM 加 SRT 低频残差构成，方向始终来自 SfM。

**技术栈：** Python 3.11、NumPy、现有 `cadscene.srt`、`cadscene.sfm.trajectory`、`ArtifactManager`、pytest；不新增运行依赖。

## 全局约束

- 不修改 no-SRT 的算法输出或正式 Job Runner 路由。
- 不覆盖 `02_sfm/camera_trajectory.json`；新输出仅写入 `02_srt/` 和 `02_fusion/`。
- 真实视频、CAD、SRT、GPS、`data/`、`runs/`、`project/` 不提交 Git。
- 同步优先 `02_sfm/frame_timestamps.csv` PTS；CFR 才可使用 `frame_index/fps` 回退。
- `rel_alt` 不得作为 WGS84 ECEF 椭球高；水平 ENU 与相对 Up 分开计算。
- 标准 Umeyama 不缩放 z；`vertical_weight` 仅用于评分、模型选择和残差组合。
- `fusion_confidence` 仅表示内部一致性，绝不表达绝对定位精度。

---

### Task 1：补齐 SfM 抽帧 PTS 产物与同步数据模型

**文件：**
- Modify: `cadscene/sfm/reconstruction.py`
- Create: `cadscene/srt/trajectory.py`
- Create: `cadscene/srt/synchronization.py`
- Modify: `cadscene/srt/__init__.py`
- Test: `tests/srt/test_synchronization.py`
- Test: `tests/sfm/test_reconstruction.py`

**接口：**
- 产生 `frame_timestamps.csv`，字段：`frame_index,pts_time_sec,timestamp_source`。
- 产生 `FrameSrtSample`：帧时间、SRT 时间、lat/lon、rel/abs/selected height、有效性、插值来源。
- 公开 `load_frame_timestamps(path)`, `resolve_frame_times(...)`, `sample_srt_at_frames(...)`。

- [ ] **Step 1：写失败测试**

```python
def test_pts_timestamp_has_priority_over_cfr_fallback() -> None:
    times = resolve_frame_times([0, 10], fps=29.97, pts_by_frame={0: 0.0, 10: 0.401})
    assert times[10].time_sec == 0.401
    assert times[10].source == "pts_csv"

def test_outside_srt_coverage_is_not_extrapolated() -> None:
    samples = sample_srt_at_frames(records(), frame_times=[FrameTime(40, 4.0, "pts_csv")])
    assert samples[0].gps_valid is False
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/srt/test_synchronization.py -q`

预期：因 `resolve_frame_times`、`FrameTime` 和 `sample_srt_at_frames` 尚不存在而失败。

- [ ] **Step 3：最小实现**

```python
@dataclass(frozen=True)
class FrameTime:
    frame_index: int
    time_sec: float
    source: str  # pts_csv | demux_pts | cfr_fps

def resolve_frame_times(frames, *, fps, pts_by_frame=None, time_scale=1.0, time_offset_sec=0.0):
    # pts_by_frame 优先；没有时仅由已确认的 CFR 调用方传入 fps 回退。
    ...

def sample_srt_at_frames(records, *, frame_times, max_interpolation_gap_sec):
    # 仅在相邻记录间隔满足阈值时线性插值；其他情况返回 gps_valid=False。
    ...
```

在抽帧阶段写入 `02_sfm/frame_timestamps.csv`。如果抽帧后端无法提供 PTS，则写明来源并让 CLI 在旧 run 中走解封装/CFR 检查分支。

- [ ] **Step 4：验证通过**

运行：`python -m pytest tests/srt/test_synchronization.py tests/sfm/test_reconstruction.py -q`

预期：PTS 优先、非整数 FPS、offset/scale、覆盖外无效、最大插值空洞和 CSV schema 全部通过。

- [ ] **Step 5：提交**

```powershell
git add cadscene/sfm/reconstruction.py cadscene/srt/trajectory.py cadscene/srt/synchronization.py cadscene/srt/__init__.py tests/srt/test_synchronization.py tests/sfm/test_reconstruction.py
git commit -m "feat: add PTS-based SRT frame synchronization"
```

### Task 2：实现 WGS84 水平 ENU 与独立 Up

**文件：**
- Create: `cadscene/srt/coordinates.py`
- Test: `tests/srt/test_coordinates.py`

**接口：**
- `EnuOrigin(latitude, longitude, ecef_reference_height_m)`；`LocalEnuPoint(east_m,north_m,up_m,...)`。
- `build_local_enu(samples, height_source="auto") -> LocalTrajectory`。

- [ ] **Step 1：写失败测试**

```python
def test_east_north_uses_fixed_ecef_reference_and_rel_alt_only_controls_up() -> None:
    points, meta = build_local_enu(samples_with_rel_alt())
    assert points[0].east_m == pytest.approx(0.0)
    assert points[1].up_m == pytest.approx(3.0)
    assert meta["ecef_reference_height_source"] == "first_abs_alt"
    assert meta["up_source"] == "rel_alt_relative"
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/srt/test_coordinates.py -q`

预期：因坐标模块不存在而失败。

- [ ] **Step 3：最小实现**

```python
def geodetic_to_ecef(latitude_deg: float, longitude_deg: float, ellipsoid_height_m: float) -> np.ndarray:
    # WGS84 a 与 e² 的标准 ECEF 公式。
    ...

def build_local_enu(samples, *, height_source: str = "auto"):
    # lat/lon 使用固定参考高度；up 用 rel_alt-first_rel_alt 或 abs_alt-first_abs_alt。
    # 返回 horizontal_datum、enu_origin、up_axis、up_source、absolute_elevation_available。
    ...
```

- [ ] **Step 4：验证通过**

运行：`python -m pytest tests/srt/test_coordinates.py -q`

预期：赤道附近东向/北向合成偏移、相对 Up、abs_alt 回退和元数据均通过。

- [ ] **Step 5：提交**

```powershell
git add cadscene/srt/coordinates.py tests/srt/test_coordinates.py
git commit -m "feat: add WGS84 local ENU conversion"
```

### Task 3：建立质量统计、退化拒绝和标准 Sim3 合成测试

**文件：**
- Create: `cadscene/srt/quality.py`
- Create: `cadscene/srt/fusion.py`
- Test: `tests/srt/test_trajectory_quality.py`
- Test: `tests/srt/test_fusion.py`

**接口：**
- `FusionConfig` 集中管理所有阈值和固定 `ransac_seed`。
- `assess_fusion_readiness(samples, sfm_centers, config) -> ReadinessReport`。
- `estimate_sfm_to_srt_sim3(source, target, config) -> Sim3Fit`。

- [ ] **Step 1：写失败测试**

```python
def test_known_sim3_is_recovered_with_fixed_seed() -> None:
    fit = estimate_sfm_to_srt_sim3(source_points(), transformed_points(), FusionConfig(ransac_seed=7))
    assert fit.scale == pytest.approx(2.5, rel=1e-5)
    assert fit.inlier_ratio > 0.9

def test_rank_two_non_collinear_path_is_allowed_with_low_vertical_observability() -> None:
    report = assess_fusion_readiness(planar_arc(), planar_arc(), FusionConfig())
    assert report.accepted is True
    assert report.vertical_observability == "low"

def test_short_or_linear_path_is_rejected() -> None:
    report = assess_fusion_readiness(line_points(), line_points(), FusionConfig())
    assert report.accepted is False
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/srt/test_trajectory_quality.py tests/srt/test_fusion.py -q`

预期：因 quality/Sim3 接口尚不存在而失败。

- [ ] **Step 3：最小实现**

```python
@dataclass(frozen=True)
class FusionConfig:
    min_common_frames: int = 12
    min_baseline_m: float = 5.0
    vertical_weight: float = 0.5
    ransac_seed: int = 20260717
    spatial_dedupe_m: float = 0.75

def estimate_sfm_to_srt_sim3(source, target, config):
    # 时间均匀采样 + 空间去重；原始 3D Umeyama 拟合。
    # vertical_weight 仅用于内点评分；返回 xy/z/combined 统计、inlier mask。
    ...
```

质量报告输出共同帧、有效率、独立位置数、XY/3D baseline、长度、重叠、重复率、最大速度/高度跳变、rank、奇异值、线性指标、`vertical_observability`、warnings。RANSAC 采样使用 `numpy.random.default_rng(config.ransac_seed)`。

- [ ] **Step 4：验证通过**

运行：`python -m pytest tests/srt/test_trajectory_quality.py tests/srt/test_fusion.py -q`

预期：Sim3 恢复、固定种子、outlier 拒绝、rank<2/直线/短 baseline 拒绝、rank=2 非共线允许、XY/Z 分离残差通过。

- [ ] **Step 5：提交**

```powershell
git add cadscene/srt/quality.py cadscene/srt/fusion.py tests/srt/test_trajectory_quality.py tests/srt/test_fusion.py
git commit -m "feat: add robust partial-SRT Sim3 estimation"
```

### Task 4：实现残差融合、四元数变换和兼容 trajectory 写出

**文件：**
- Modify: `cadscene/srt/fusion.py`
- Modify: `cadscene/srt/trajectory.py`
- Test: `tests/srt/test_fusion.py`
- Test: `tests/sfm/test_trajectory.py`

**接口：**
- `fuse_positions(...) -> FusionResult`。
- `rotate_cam_from_world_quat(quat_wxyz, r_sim3) -> list[float]`。
- `build_fused_trajectory_json(raw_json, result) -> dict`。

- [ ] **Step 1：写失败测试**

```python
def test_srt_jump_is_not_copied_to_fused_position() -> None:
    result = fuse_positions(metric_sfm(), srt_with_one_jump(), config=FusionConfig(smoothing_window_sec=2.0))
    assert np.linalg.norm(result.fused_positions[5] - result.metric_positions[5]) < 3.0

def test_non_identity_sim3_rotation_preserves_camera_projection() -> None:
    new_quat = rotate_cam_from_world_quat(old_quat, rotation_z_90())
    assert np.allclose(quat_wxyz_to_matrix(new_quat) @ transformed_world_point, old_matrix @ old_world_point)
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/srt/test_fusion.py tests/sfm/test_trajectory.py -q`

预期：因融合和四元数接口缺失而失败。

- [ ] **Step 3：最小实现**

```python
def fuse_positions(metric_positions, frame_times, srt_samples, fit, config):
    # 仅在连续 SRT coverage 内用 rolling median residual；空洞/coverage 外不外推修正。
    # support < min_smoothing_support 时保留 metric SfM 并给出低 confidence。
    ...

def rotate_cam_from_world_quat(quat_wxyz, r_sim3):
    return matrix_to_quat_wxyz(quat_wxyz_to_matrix(quat_wxyz) @ r_sim3.T)
```

输出 pose 的原有字段不变；新增 `srt_valid`、`srt_interpolated`、`srt_residual_m`、`fusion_confidence`、组成指标。顶层 meta 写明 horizontal datum、ENU origin、up axis/source、absolute elevation、Sim3、PTS 参数和 `orientation_source=sfm`。

- [ ] **Step 4：验证通过**

运行：`python -m pytest tests/srt/test_fusion.py tests/sfm/test_trajectory.py -q`

预期：跳点抑制、空洞不外推、低频修正、旋转投影等价性、旧 loader 兼容和 NaN 拒绝通过。

- [ ] **Step 5：提交**

```powershell
git add cadscene/srt/fusion.py cadscene/srt/trajectory.py tests/srt/test_fusion.py tests/sfm/test_trajectory.py
git commit -m "feat: export fused SRT and SfM trajectories"
```

### Task 5：实现独立 CLI、报告和诊断导出

**文件：**
- Create: `cadscene/cli/fuse_srt_sfm.py`
- Modify: `cadscene/srt/__init__.py`
- Create: `tests/cli/test_fuse_srt_sfm_cli.py`

**接口：**
- `main(argv: list[str] | None = None) -> int`。
- 成功写入 `02_srt/` 的 trajectory/sample/sync stats/report，及 `02_fusion/` 的 fused JSON/CSV/stats/report/comparison CSV。

- [ ] **Step 1：写失败测试**

```python
def test_cli_writes_fusion_artifacts_and_prints_metrics(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "--trajectory", str(trajectory), "--srt", str(srt), "--video", str(video))
    assert result.returncode == 0
    assert (run_dir / "02_fusion/camera_trajectory_fused.json").exists()
    assert "Sim3 RMSE" in result.stdout

def test_cli_rejects_insufficient_baseline_without_fused_output(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "--min-baseline-m", "5")
    assert result.returncode != 0
    assert "可回退到 sfm_only" in result.stderr
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/cli/test_fuse_srt_sfm_cli.py -q`

预期：因 CLI 模块不存在而失败。

- [ ] **Step 3：最小实现**

```python
parser.add_argument("--time-scale", type=float, default=1.0)
parser.add_argument("--time-offset-sec", type=float, default=0.0)
parser.add_argument("--max-interpolation-gap-sec", type=float, default=1.5)
parser.add_argument("--min-smoothing-support", type=int, default=3)
parser.add_argument("--vertical-weight", type=float, default=0.5)
```

显式 `--trajectory/--srt/--video` 优先；否则安全读取 dataset manifest。失败不创建 fused JSON。报告以中文写入，并包含 PTS 来源、水平/Up datum、样本去重数、inlier 比例、XY/Z/combined 残差、baseline、低置信区间、warnings 和限制。

- [ ] **Step 4：验证通过**

运行：`python -m pytest tests/cli/test_fuse_srt_sfm_cli.py -q; python -m cadscene.cli.fuse_srt_sfm --help`

预期：成功产物 schema、失败拒绝、显式参数优先、help 和中文报告通过。

- [ ] **Step 5：提交**

```powershell
git add cadscene/cli/fuse_srt_sfm.py cadscene/srt/__init__.py tests/cli/test_fuse_srt_sfm_cli.py
git commit -m "feat: add partial-SRT fusion CLI"
```

### Task 6：验证 `align_to_cad` 接入与完整回归

**文件：**
- Test: `tests/integration/test_partial_srt_alignment.py`
- Modify: `tests/cli/test_align_to_cad_cli.py`（仅在现有显式 `--trajectory` smoke 缺少 fused schema 覆盖时）
- Modify: `docs/srt_capability_detection.md`
- Modify: `docs/workflow_routing.md`

**接口：**
- 融合 JSON 通过既有 `load_sfm_trajectory` 和 `align_to_cad --trajectory`，产生 `03_alignment` 标准产物。

- [ ] **Step 1：写失败测试**

```python
def test_fused_trajectory_runs_existing_align_to_cad(tmp_path: Path) -> None:
    fused = write_compatible_fused_trajectory(tmp_path)
    result = run_align_cli(tmp_path, trajectory=fused)
    assert result.returncode == 0
    assert (tmp_path / "runs/demo/r1/03_alignment/alignment.json").exists()
```

- [ ] **Step 2：验证失败**

运行：`python -m pytest tests/integration/test_partial_srt_alignment.py -q`

预期：在融合 JSON 尚未可用或 schema 缺字段时失败。

- [ ] **Step 3：最小实现与文档更新**

不改变 `align_to_cad` 的 CAD Sim3 语义；若其 loader 已能读取 fused schema，不改生产代码。文档明确 partial-SRT 输出仍是 ENU，下一步仍需要人工关键帧/CAD alignment，且 `fusion_confidence` 非绝对精度。

- [ ] **Step 4：完整验证**

运行：

```powershell
python -m pytest tests/srt tests/cli/test_fuse_srt_sfm_cli.py tests/integration/test_partial_srt_alignment.py -q
python -m pytest
python scripts/check_no_project_dependency.py
python -m cadscene.cli.fuse_srt_sfm --help
```

预期：聚焦融合测试通过；全量测试仅允许已记录的 `project/tests` 旧依赖收集错误，其他测试不得新增失败；独立性检查和 CLI help 通过。

- [ ] **Step 5：真实数据 smoke（不提交数据）**

先对本地真实数据运行 0–250 帧 SfM，再运行 CLI；检查 `srt_sync_report.md` 与 `fusion_report.md` 中的 PTS 来源、共同帧、重叠、去重数、inlier 比例、XY/Z residual、baseline、低置信区间。对比 sfm_only、metric SfM、raw SRT 和 fused 轨迹，再使用同一人工关键帧运行 `align_to_cad`。不以“看起来平滑”作为验收结论。

- [ ] **Step 6：提交与推送**

```powershell
git add tests/integration/test_partial_srt_alignment.py tests/cli/test_align_to_cad_cli.py docs/srt_capability_detection.md docs/workflow_routing.md
git commit -m "test: verify partial-SRT fusion CAD alignment"
git push -u origin feature/partial-srt-fusion-core
```

## 计划自检

- 设计要求对应：Task 1（PTS、同步空洞）、Task 2（水平/Up）、Task 3（质量、退化、标准 Sim3）、Task 4（融合和方向）、Task 5（CLI/报告）、Task 6（CAD/no-SRT 回归/真实 smoke）。
- 无第二 SRT parser、无前端/Job Runner正式接入、无 GPS→CAD 自动投影。
- 所有新增行为先有失败测试，再实现、验证和提交。
