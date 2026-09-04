# SRT 位姿先验六自由度微调与重建姿态恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让完整姿态 SRT 与缺失姿态 SRT 共用六自由度关键帧微调、路线拟合和渲染工作台，同时用官方 COLMAP 稀疏三维重建取代缺失姿态路线中的 OpenCV 姿态估计。

**Architecture:** 两类 SRT 都先发布不可变的 CAD 米制基准位姿，再由独立的 pose-prior residual 核心拟合人工关键帧的 XYZ 与旋转残差。完整 SRT 直接生成基准位姿；缺失姿态 SRT 先运行现有 COLMAP sparse pipeline，再把已注册相机与同帧 SRT 中心鲁棒配准，只转移旋转并逐帧强制保留 SRT 中心。现有 alignment、viewer scene 和 renderer 协议保持兼容，普通 SfM 分支不变。

**Tech Stack:** Python 3.11、NumPy、SciPy `Rotation`/`Slerp`、官方 COLMAP CLI 4.1、pytest、原生 JavaScript/Three.js。

## Global Constraints

- 完整姿态 SRT 不运行特征、匹配、COLMAP 或姿态反算。
- 缺失姿态 SRT 的生产路径必须运行官方 COLMAP 稀疏重建；不得调用 ORB/Essential Matrix/`recoverPose`，也不得回退 `sfm_only`。
- 缺失姿态路线最终每帧中心必须与 SRT→CAD 中心一致；COLMAP 中心只允许用于模型配准和诊断。
- 水平 FOV 是用户输入的整段全局整数参数；首版将它作为 COLMAP 相机内参并关闭焦距 BA。
- 人工微调允许每个关键帧修改 `x/y/z/yaw/pitch/roll`；CAD 坐标系、CAD 比例和 georeference 保持锁定。
- 位置残差线性插值，旋转残差用四元数 SLERP，区间外保持端点残差。
- 缺失姿态路线可展示配准后的稀疏点云，但点云不作为拟合或渲染门禁。
- 两条 SRT 路线不进入旧质量检测阶段；微调、路线拟合和渲染保持独立阶段。
- 现有 `sfm_only`、`pure_rotation` 和历史 `srt_sfm_fused` 行为必须保持不变。

---

### Task 1: 建立位姿先验残差数学核心

**Files:**
- Create: `cadscene/alignment/pose_prior_refinement.py`
- Create: `tests/alignment/test_pose_prior_refinement.py`

**Interfaces:**
- Consumes: 基准帧 `PosePrior(frame_index: int, center: ndarray[3], world_from_camera: ndarray[3,3])` 与人工帧 `ManualPose(frame_index: int, center: ndarray[3], world_from_camera: ndarray[3,3])`。
- Produces: `fit_pose_prior_residuals(base_poses, manual_poses) -> PosePriorResidualFit`；`PosePriorResidualFit.pose_at(frame_index) -> tuple[ndarray, ndarray]`。

- [ ] **Step 1: Write the failing tests**

```python
def test_zero_keys_returns_base_pose():
    fit = fit_pose_prior_residuals(_base(), [])
    center, rotation = fit.pose_at(10)
    np.testing.assert_allclose(center, [10.0, 0.0, 2.0])
    np.testing.assert_allclose(rotation, np.eye(3), atol=1e-10)

def test_two_keys_interpolate_position_and_rotation_residuals():
    base = _base(frames=(0, 10), centers=((0, 0, 0), (10, 0, 0)))
    manual = [
        _manual(0, center=(1, 0, 0), yaw=170),
        _manual(10, center=(13, 2, 0), yaw=-170),
    ]
    fit = fit_pose_prior_residuals(base, manual)
    center, rotation = fit.pose_at(5)
    np.testing.assert_allclose(center, [7.0, 1.0, 0.0])
    assert abs(_yaw(rotation) - 180.0) < 1e-6

def test_one_key_holds_six_dof_residual_for_whole_route():
    fit = fit_pose_prior_residuals(_base(), [_manual(10, center=(12, 3, 4), yaw=25)])
    np.testing.assert_allclose(fit.pose_at(0)[0], [2, 3, 2])
    np.testing.assert_allclose(fit.pose_at(20)[0], [22, 3, 2])
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/alignment/test_pose_prior_refinement.py -q`

Expected: collection fails because `cadscene.alignment.pose_prior_refinement` does not exist.

- [ ] **Step 3: Implement the residual model**

```python
@dataclass(frozen=True)
class PosePrior:
    frame_index: int
    center: np.ndarray
    world_from_camera: np.ndarray

@dataclass(frozen=True)
class ManualPose:
    frame_index: int
    center: np.ndarray
    world_from_camera: np.ndarray

@dataclass(frozen=True)
class PosePriorResidualFit:
    base_frames: np.ndarray
    base_centers: np.ndarray
    base_rotations: Rotation
    key_frames: np.ndarray
    position_residuals: np.ndarray
    rotation_residuals: Rotation

    def pose_at(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        base_center, base_rotation = _interpolate_base(self, frame_index)
        delta_center, delta_rotation = _interpolate_residual(self, frame_index)
        return base_center + delta_center, delta_rotation @ base_rotation

def fit_pose_prior_residuals(
    base_poses: Sequence[PosePrior], manual_poses: Sequence[ManualPose]
) -> PosePriorResidualFit:
    # validate finite, unique, sorted frame identities; use
    # delta_R = R_manual @ R_base.T and SciPy Slerp for interpolation.
```

The implementation must normalize rotations through `Rotation.from_matrix`, preserve exact manual poses at keys, return identity residuals for zero keys, and hold the only/endpoint residual outside the key range.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/alignment/test_pose_prior_refinement.py -q`

Expected: all tests pass, including quaternion sign and ±179° wrap cases.

- [ ] **Step 5: Commit**

```bash
git add cadscene/alignment/pose_prior_refinement.py tests/alignment/test_pose_prior_refinement.py
git commit -m "feat: add SRT pose-prior residual fitting"
```

### Task 2: 路由 SRT alignment 到残差拟合器

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Modify: `tests/alignment/test_aligner.py`

**Interfaces:**
- Consumes: Task 1 `fit_pose_prior_residuals`；`SfmTrajectory` 中 `meta.pose_prior_schema == "srt_pose_prior_v1"`。
- Produces: `AnchoredAlignment` 的 `position_mode="srt_pose_prior_residual"`，逐帧 `residual_positions` 与旋转矩阵插值；外部 alignment JSON/CSV 路径不变。

- [ ] **Step 1: Write failing alignment tests**

```python
def test_full_pose_accepts_nonuniform_xyz_and_attitude_keyframes(tmp_path):
    trajectory = _write_srt_pose_prior(tmp_path, workflow="srt_full_pose")
    manual = _manual_track(
        workflow="srt_full_pose",
        rows=[_row(0, x=1, yaw=5), _row(10, x=14, yaw=25)],
    )
    result = align_from_keyframes(trajectory, manual, _config())
    assert result.anchored.position_mode == "srt_pose_prior_residual"
    np.testing.assert_allclose(result.state_at(5).camera_x, 7.5)

def test_fixed_track_final_centers_follow_srt_plus_manual_residual(tmp_path):
    trajectory = _write_srt_pose_prior(tmp_path, workflow="srt_fixed_track_visual_pose")
    result = align_from_keyframes(trajectory, _manual_track(rows=[]), _config())
    np.testing.assert_allclose(result.state_at(5).center, trajectory.center_at(5))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/alignment/test_aligner.py -k "pose_prior or nonuniform" -q`

Expected: full-pose validation rejects attitude/non-uniform XYZ or returns `metric_direct`.

- [ ] **Step 3: Implement the SRT branch**

```python
def _is_srt_pose_prior(traj: SfmTrajectory) -> bool:
    return traj.meta.get("pose_prior_schema") == "srt_pose_prior_v1"

def _pose_prior_alignment(track, traj, config):
    base = [
        PosePrior(int(frame), traj.center_at(frame), traj.orientation_at(frame).T)
        for frame in traj.frames
        if traj.is_orientation_available(frame)
    ]
    manual = [_manual_pose(row, config) for row in confirmed_keyframes(dict(track))]
    fit = fit_pose_prior_residuals(base, manual)
    return _anchored_alignment_from_pose_prior_fit(fit)
```

Route only new SRT metadata to this branch. Remove `uniform_xyz_offset_only` validation for the new schema but retain it for legacy read-only revisions. Do not modify the existing ordinary Sim3, pure rotation, or legacy fixed-track branches.

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m pytest tests/alignment/test_pose_prior_refinement.py tests/alignment/test_aligner.py -q`

Expected: all pass; old uniform-only historical tests remain valid under legacy metadata.

- [ ] **Step 5: Commit**

```bash
git add cadscene/alignment/aligner.py tests/alignment/test_aligner.py
git commit -m "feat: fit SRT tracks from six-DoF residual keys"
```

### Task 3: 发布可编辑的完整 SRT 位姿先验

**Files:**
- Modify: `cadscene/srt/full_pose_workbench.py`
- Modify: `cadscene/srt/full_pose.py`
- Modify: `tests/srt/test_full_pose_workbench.py`
- Modify: `tests/cli/test_build_srt_full_pose_cli.py`

**Interfaces:**
- Consumes: 完整姿态 trajectory 的已注册 poses。
- Produces: trajectory meta `pose_prior_schema="srt_pose_prior_v1"`、`edit_policy="six_dof_keyframe_residuals"`；预测轨迹保留自动 pose，人工轨迹初始为空。

- [ ] **Step 1: Write failing contract tests**

```python
def test_full_pose_publishes_read_only_prior_not_manual_keys():
    payloads = build_full_pose_workbench_payloads(_trajectory())
    assert payloads.camera_track["meta"]["pose_prior_schema"] == "srt_pose_prior_v1"
    assert payloads.camera_track["meta"]["edit_policy"] == "six_dof_keyframe_residuals"
    assert all("position_locked" not in row for row in payloads.camera_track["keyframes"])
    assert all(row["source"] == "algorithm_prediction" for row in payloads.camera_track["keyframes"])
    assert payloads.viewer_scene["meta"]["point_cloud_generated"] is False
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/srt/test_full_pose_workbench.py tests/cli/test_build_srt_full_pose_cli.py -q`

Expected: assertions fail on `uniform_xyz_offset_only`, `position_locked`, or missing schema.

- [ ] **Step 3: Update full-pose metadata and prediction source**

```python
POSE_PRIOR_META = {
    "pose_prior_schema": "srt_pose_prior_v1",
    "metric_scale_locked": True,
    "position_source": "srt_cad",
    "orientation_source": "srt_full_pose",
    "edit_policy": "six_dof_keyframe_residuals",
}
```

Merge this metadata into trajectory, camera prediction, and viewer scene. Keep the automatic route in `global_sfm_track`, remove `position_locked`, label prediction rows `source="algorithm_prediction"`, and never write automatic rows into `camera_track_manual.json`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/srt/test_full_pose_workbench.py tests/cli/test_build_srt_full_pose_cli.py tests/integration/test_srt_full_pose_workflow.py -q`

Expected: all pass and integration still proves no reconstruction command is present.

- [ ] **Step 5: Commit**

```bash
git add cadscene/srt/full_pose_workbench.py cadscene/srt/full_pose.py tests/srt/test_full_pose_workbench.py tests/cli/test_build_srt_full_pose_cli.py
git commit -m "feat: publish editable full-SRT pose priors"
```

### Task 4: 让缺失姿态工作流运行官方 COLMAP 并使用用户 FOV

**Files:**
- Modify: `cadscene/sfm/reconstruction.py`
- Modify: `cadscene/sfm/colmap_cli.py`
- Modify: `cadscene/cli/run_sfm.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `tests/sfm/test_colmap_cli_backend.py`
- Modify: `tests/cli/test_run_sfm_backend_cli.py`
- Modify: `tests/projects/test_workflow_adapters.py`

**Interfaces:**
- Consumes: `FixedTrackVisualPoseConfig.horizontal_fov_deg` 与 video width/height。
- Produces: `ReconstructionConfig.camera_params: tuple[float, ...] | None`、`ReconstructionConfig.refine_focal_length: bool`；缺失姿态 adapter commands 为 `run_sfm` 后接 builder。

- [ ] **Step 1: Write failing command tests**

```python
def test_colmap_cli_uses_authoritative_fov_intrinsics_and_disables_focal_ba():
    commands = build_colmap_cli_commands(
        "colmap",
        _paths(),
        camera_model="PINHOLE",
        camera_params=(1000.0, 1000.0, 960.0, 540.0),
        refine_focal_length=False,
        max_image_size=2048,
        max_num_features=12000,
        sequential_overlap=15,
        init_min_tri_angle=2.0,
        ba_global_frames_ratio=2.0,
        ba_global_points_ratio=2.0,
        ba_global_frames_freq=1000,
        ba_global_points_freq=1_000_000,
        ba_global_max_num_iterations=25,
        ba_global_max_refinements=2,
        use_mask=False,
        use_gpu=False,
        gpu_index="0",
        feature_help="--FeatureExtraction.max_image_size --FeatureExtraction.max_num_features --FeatureExtraction.use_gpu --FeatureExtraction.gpu_index",
        matching_help="--FeatureMatching.use_gpu --FeatureMatching.gpu_index",
    )
    joined = " ".join(" ".join(command) for command in commands)
    assert "--ImageReader.camera_params 1000,1000,960,540" in joined
    assert "--Mapper.ba_refine_focal_length 0" in joined

def test_missing_pose_adapter_runs_sparse_reconstruction_before_pose_transfer(inputs):
    commands = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    ).build_commands(inputs)
    assert commands[0][1:3] == ("-m", "cadscene.cli.run_sfm")
    assert "--backend" in commands[0] and "colmap_cli" in commands[0]
    assert commands[1][1:3] == ("-m", "cadscene.cli.build_srt_fixed_track_visual_pose")
    assert "--reconstruction-trajectory" in commands[1]
    assert "--sparse-ply" in commands[1]
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/sfm/test_colmap_cli_backend.py tests/cli/test_run_sfm_backend_cli.py tests/projects/test_workflow_adapters.py -k "fov or missing_pose" -q`

Expected: config fields/CLI switches are absent and fixed-track adapter contains only one command.

- [ ] **Step 3: Extend reconstruction configuration**

```python
@dataclass(frozen=True)
class ReconstructionConfig:
    camera_model: str = "OPENCV"
    camera_params: tuple[float, ...] | None = None
    refine_focal_length: bool = True

parser.add_argument("--camera-params")
parser.add_argument("--no-refine-focal-length", action="store_true")
```

Parse the comma-separated values as finite positive intrinsics, pass them to COLMAP feature extraction through `--ImageReader.camera_params`, and pass `0/1` to `--Mapper.ba_refine_focal_length`. Existing callers with `None/True` retain current behavior.

- [ ] **Step 4: Build the two-command fixed-track adapter**

```python
ExistingWorkflowAdapter(
    name="srt_fixed_track_visual_pose",
    version="2",
    srt_requirement="fixed_track",
    modules=("cadscene.cli.run_sfm", "cadscene.cli.build_srt_fixed_track_visual_pose"),
    output_relative_path="02_srt_visual_pose/camera_trajectory_visual_pose.json",
)
```

For this workflow `_sfm_command` must force official `colmap_cli`, calculate `fx=width/(2*tan(fov/2))`, pass `PINHOLE` params `fx,fx,cx,cy`, disable focal refinement, retain sparse point output, and use the existing frame step. `_fixed_track_command` must append the two paths under `02_sfm`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python -m pytest tests/sfm/test_colmap_cli_backend.py tests/cli/test_run_sfm_backend_cli.py tests/projects/test_workflow_adapters.py -q`

Expected: all pass and normal SfM command construction is unchanged.

- [ ] **Step 6: Commit**

```bash
git add cadscene/sfm/reconstruction.py cadscene/sfm/colmap_cli.py cadscene/cli/run_sfm.py cadscene/projects/workflow_adapters.py tests/sfm/test_colmap_cli_backend.py tests/cli/test_run_sfm_backend_cli.py tests/projects/test_workflow_adapters.py
git commit -m "feat: run COLMAP for missing SRT attitudes"
```

### Task 5: 把 COLMAP 姿态和稀疏点配准到 SRT/CAD

**Files:**
- Create: `cadscene/srt/colmap_pose_transfer.py`
- Create: `tests/srt/test_colmap_pose_transfer.py`
- Modify: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Modify: `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`
- Modify: `tests/integration/test_srt_fixed_track_visual_pose_workflow.py`

**Interfaces:**
- Consumes: `SfmTrajectory` COLMAP trajectory、`Sequence[FixedTrackPosition]`、optional sparse PLY。
- Produces: `transfer_colmap_pose_to_srt(...) -> ColmapPoseTransfer`，其中 `rotations` 用 `cam_from_world`，`centers` 永远来自 SRT，`sim3` 仅用于姿态与点云转换。

- [ ] **Step 1: Write failing synthetic transfer tests**

```python
def test_transfer_uses_colmap_rotation_but_exact_srt_centers():
    reconstruction = _synthetic_colmap_model(scale=2.0, yaw_deg=35.0)
    positions = _srt_positions()
    result = transfer_colmap_pose_to_srt(reconstruction, positions)
    for row in positions:
        np.testing.assert_array_equal(result.centers[row.frame_index], row.center)
        np.testing.assert_allclose(
            result.world_from_camera[row.frame_index],
            _expected_world_from_camera(row.frame_index),
            atol=1e-6,
        )

def test_transfer_rejects_degenerate_or_high_residual_alignment():
    with pytest.raises(ColmapPoseTransferError, match="配准"):
        transfer_colmap_pose_to_srt(_collinear_two_frame_model(), _bad_positions())
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/srt/test_colmap_pose_transfer.py -q`

Expected: module import fails.

- [ ] **Step 3: Implement robust center registration and rotation transfer**

```python
@dataclass(frozen=True)
class ColmapPoseTransfer:
    centers: dict[int, np.ndarray]
    world_from_camera: dict[int, np.ndarray]
    sim3: Sim3
    registered_frames: tuple[int, ...]
    unregistered_frames: tuple[int, ...]
    alignment_residuals_m: dict[int, float]

def transfer_colmap_pose_to_srt(
    reconstruction: SfmTrajectory,
    positions: Sequence[FixedTrackPosition],
    *, max_alignment_residual_m: float = 20.0,
) -> ColmapPoseTransfer:
    pairs = _same_frame_center_pairs(reconstruction, positions)
    sim3, inliers = _robust_umeyama(pairs)
    # world_from_camera_cad = sim3.rotation @ world_from_camera_colmap
    # centers dict is constructed only from position.center, never sim3.apply(center).
```

Use deterministic leave-one-out/RANSAC sampling for outliers, require at least three non-degenerate paired centers, and report residuals. Transform sparse points with the fitted Sim3 into viewer coordinates through existing `load_ply` and point export helpers.

- [ ] **Step 4: Replace the OpenCV production call in the builder**

```python
parser.add_argument("--reconstruction-trajectory", required=True, type=Path)
parser.add_argument("--sparse-ply", type=Path)

reconstruction = load_sfm_trajectory(args.reconstruction_trajectory)
transfer = transfer_colmap_pose_to_srt(reconstruction, positions)
solution = orientation_solution_from_colmap_transfer(transfer, positions)
```

Remove the import and call to `estimate_video_orientations` from this CLI. Keep the old module only for legacy readers/tests until a later deletion. Publish `orientation_source="colmap_sparse_srt_aligned"`, `pose_prior_schema="srt_pose_prior_v1"`, `edit_policy="six_dof_keyframe_residuals"`, reconstruction coverage and alignment residuals. Scene points are diagnostic and optional; final pose centers are copied directly from `FixedTrackPosition.center`.

- [ ] **Step 5: Run focused/integration tests and verify GREEN**

Run: `python -m pytest tests/srt/test_colmap_pose_transfer.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

Expected: all pass; integration command list contains COLMAP reconstruction first and no builder monkeypatch of `estimate_video_orientations`.

- [ ] **Step 6: Commit**

```bash
git add cadscene/srt/colmap_pose_transfer.py cadscene/cli/build_srt_fixed_track_visual_pose.py tests/srt/test_colmap_pose_transfer.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py
git commit -m "feat: transfer COLMAP attitudes onto SRT tracks"
```

### Task 6: 统一 SRT 六自由度工作台、实时视锥与阶段文案

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/keyframe_sources.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/index.html`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `tests/viewer/test_workflow_ui_static.py`
- Modify: `tests/viewer/test_keyframe_sources.py`
- Modify: `tests/viewer/test_full_pose_adjustment_node.py`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `tests/workflow/test_job_runner.py`

**Interfaces:**
- Consumes: trajectory/viewer metadata from Tasks 3 and 5.
- Produces: shared SRT UI stages `基准轨迹 → 关键帧微调 → 路线拟合 → 渲染导出` and live frustum driven by fitted pose.

- [ ] **Step 1: Write failing UI contract tests**

```python
def test_both_srt_workflows_use_six_dof_keyframe_controls():
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert "isSrtPosePriorWorkflow" in script
    assert "six_dof_keyframe_residuals" in script
    assert "fullPoseAdjustmentPanel" not in script

def test_missing_pose_copy_names_sparse_reconstruction():
    workspace = Path("apps/project_workspace/project_workspace.js").read_text(encoding="utf-8")
    assert 'srt_fixed_track_visual_pose: "SRT 轨迹 + 稀疏重建姿态"' in workspace
    assert "稀疏重建恢复姿态" in workspace
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py tests/viewer/test_keyframe_sources.py tests/viewer/test_project_workspace_static.py tests/workflow/test_job_runner.py -q`

Expected: old full-pose XYZ panel/visual-pose copy is still asserted or present.

- [ ] **Step 3: Consolidate SRT workflow UI**

```javascript
function isSrtPosePriorWorkflow(mode = workflowMode()) {
  return mode === "srt_full_pose" || mode === "srt_fixed_track_visual_pose";
}

const SRT_STAGES = ["基准轨迹", "关键帧微调", "路线拟合", "渲染导出"];
```

Remove the dedicated uniform-XYZ panel and its handlers. Show existing translation/rotation gizmos, numeric XYZ/yaw/pitch/roll, global integer FOV, keyframe plan/navigation, fit/save and independent render buttons for both SRT workflows. Automatic prediction rows remain base-track sources and are not counted as confirmed manual keys. Drive the current frustum from the fitted path when available, otherwise from the base prior; keep gray base path and orange fitted path visible.

- [ ] **Step 4: Correct task status copy and progress**

For `srt_full_pose`, report `读取 SRT 位姿`; for `srt_fixed_track_visual_pose`, report `抽取重建帧 / COLMAP 稀疏重建 / 姿态配准到 SRT / 准备工作台`. Never show the old OpenCV fixed-center copy or ordinary `SfM 重建` current-task label for SRT modes.

- [ ] **Step 5: Run UI/workflow tests and verify GREEN**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py tests/viewer/test_keyframe_sources.py tests/viewer/test_full_pose_adjustment_node.py tests/viewer/test_project_workspace_static.py tests/workflow/test_job_runner.py -q`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/web_camera_viewer apps/project_workspace cadscene/workflow/job_runner.py tests/viewer tests/workflow/test_job_runner.py
git commit -m "feat: unify SRT six-DoF refinement workbench"
```

### Task 7: 渲染恢复、回归和真实数据 smoke

**Files:**
- Modify: `cadscene/projects/workbench_render_adapter.py`
- Modify: `cadscene/projects/service.py`
- Modify: `tests/projects/test_render_adapters.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/projects/test_executor.py`
- Modify: `docs/workflow_routing.md`

**Interfaces:**
- Consumes: saved `srt_pose_residual_keyframes_v1` manual track and latest fitted alignment.
- Produces: restart-safe SRT workbench restoration and render job that consumes the fitted path without rerunning the baseline generator.

- [ ] **Step 1: Write failing service tests**

```python
@pytest.mark.parametrize("workflow", ["srt_full_pose", "srt_fixed_track_visual_pose"])
def test_srt_render_uses_saved_fitted_alignment_without_rerunning_baseline(workflow, service):
    session = _ready_srt_session(service, workflow)
    _save_two_six_dof_keys(service, session)
    fit_job = service.start_alignment_job(session.project_id, session.clip_id)
    render_job = service.start_render_job(session.project_id, session.clip_id)
    assert render_job.inputs["alignment_revision"] == fit_job.output_revision
    assert render_job.inputs["trajectory_revision"] == session.trajectory_revision
    assert service.jobs_for(operation="trajectory")[-1].id == session.trajectory_job_id
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/projects/test_render_adapters.py tests/projects/test_workbench_sessions.py tests/projects/test_executor.py -k "srt and fitted" -q`

Expected: render/session logic still assumes uniform offset or stale baseline semantics.

- [ ] **Step 3: Bind save/restore/render revisions**

Persist `edit_policy="srt_pose_residual_keyframes_v1"`, trajectory output revision/fingerprint, frame-map revision, georeference revision and integer FOV revision. Manual-key changes invalidate alignment/render only. FOV changes invalidate missing-pose reconstruction but only refresh complete-SRT baseline/view. Render adapter consumes the latest successful fitted alignment and accepts optional fixed-track diagnostic point cloud without making it required.

- [ ] **Step 4: Update operator documentation**

Document the exact user flow:

```text
完整 SRT：配置 CAD/FOV → 读取位姿 → 进入工作台 → 添加/修改 6DoF 关键帧 → 路线拟合 → 渲染。
缺失姿态 SRT：配置 CAD/FOV → COLMAP 稀疏重建 → 姿态配准到 SRT → 进入同一工作台 → 路线拟合 → 渲染。
```

State that incomplete-SRT reconstruction time can approach ordinary sparse SfM, but final positions always follow SRT and dense/quality stages are skipped.

- [ ] **Step 5: Run the regression suite**

Run: `python -m pytest tests/alignment tests/srt tests/cli/test_build_srt_full_pose_cli.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/projects/test_render_adapters.py tests/viewer/test_workflow_ui_static.py tests/viewer/test_project_workspace_static.py tests/integration/test_srt_full_pose_workflow.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

Expected: all pass.

- [ ] **Step 6: Run real-data smoke and inspect artifacts**

Run the service with `CADSCENE_STORAGE_ROOT=D:\zjic2026\cadscene_workbench\work\srt-full-pose-ui-test`, create one complete-SRT and one missing-SRT project, then verify:

```text
complete SRT command modules == {cadscene.cli.build_srt_full_pose}
missing SRT commands == cadscene.cli.run_sfm(colmap_cli) then cadscene.cli.build_srt_fixed_track_visual_pose
all final fixed-track centers exactly equal SRT→CAD centers
workbench shows base route, fitted route, live frustum and optional sparse points
saved six-DoF keys survive service restart
render starts only after a successful fitted alignment
```

- [ ] **Step 7: Commit**

```bash
git add cadscene/projects/workbench_render_adapter.py cadscene/projects/service.py tests/projects/test_render_adapters.py tests/projects/test_workbench_sessions.py tests/projects/test_executor.py docs/workflow_routing.md
git commit -m "feat: complete SRT refinement and render lifecycle"
```

### Task 8: 最终验证与工作树交付

**Files:**
- Verify only; no product-code edits unless a failing test exposes a defect.

**Interfaces:**
- Consumes: all preceding commits.
- Produces: reproducible verification evidence and a running project-library service.

- [ ] **Step 1: Scan production pose path**

Run: `rg -n "estimate_video_orientations|recoverPose|findEssentialMat" cadscene/cli/build_srt_fixed_track_visual_pose.py cadscene/projects/workflow_adapters.py`

Expected: no matches.

- [ ] **Step 2: Verify no accidental broad changes**

Run: `git diff --check && git status --short && git diff --stat HEAD~7..HEAD`

Expected: no whitespace errors; only intended product/test/docs files are committed, and pre-existing unrelated dirty files remain untouched.

- [ ] **Step 3: Run final targeted suite**

Run: `python -m pytest tests/alignment tests/srt tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/projects/test_render_adapters.py tests/viewer/test_workflow_ui_static.py tests/viewer/test_project_workspace_static.py tests/integration/test_srt_full_pose_workflow.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

Expected: all pass with zero failures.

- [ ] **Step 4: Start the service**

Start the existing project-library command on `http://127.0.0.1:8310/apps/project_library/` using the feature worktree and the existing `srt-full-pose-ui-test` storage root. Verify `GET /api/projects` returns HTTP 200 and then leave the process running for user testing.
