# SRT Fixed-Track Visual Pose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent `srt_fixed_track_visual_pose` workflow that locks every camera center to the SRT→CGCS2000→CAD track, estimates only camera rotations from video and user FOV, opens a trajectory-first workbench, and renders without SfM or a point cloud.

**Architecture:** A new SRT core module first creates an immutable per-frame position track from the confirmed CAD georeference and `rel_alt`, then a separate OpenCV rotation solver estimates relative rotations and anchors them to the known world baselines without registering camera-center variables. A versioned workflow adapter and CLI publish the trajectory, position-line viewer scene, diagnostics, and initial camera track; project and workbench services route partial SRT to this new adapter and never fall back to the legacy `srt_sfm_fused` or `sfm_only` pipelines.

**Tech Stack:** Python 3.10+, NumPy, SciPy `Rotation`, OpenCV ORB/essential-matrix recovery, pyproj through existing CAD georeference helpers, pytest, vanilla JavaScript project/workbench UI, existing project queue and render pipeline.

## Global Constraints

- New workflow key is exactly `srt_fixed_track_visual_pose`; user-facing label is exactly `SRT 轨迹 + 视觉姿态`.
- New projects and re-analysis never recommend or enqueue `srt_sfm_fused`; its adapter remains readable only for historical outputs.
- `canonical_center_xyz` is always SRT→CAD XY plus SRT `rel_alt`; `abs_alt` is diagnostic-only.
- The only position correction is one finite `route_offset_xyz_m=[x,y,z]` applied identically to every frame.
- The visual solver may optimize rotations and temporary landmarks, but camera centers are never parameters and never overwritten.
- Automatic attitude failure publishes `position_only` or `orientation_partial`; it never falls back to SfM.
- No `run_sfm`, `fuse_srt_sfm`, sparse PLY, quality job, or quality page is used by the new workflow.
- Horizontal FOV is user input, finite, and strictly inside `(1°, 179°)`.
- Position data is viewable even when no frame has a usable orientation; frustums exist only for orientation-available frames.

---

### Task 1: Introduce the independent workflow identity and routing

**Files:**
- Modify: `cadscene/srt/capability.py`
- Modify: `cadscene/video_analysis/recommendation.py`
- Modify: `cadscene/workflow/data_import.py`
- Modify: `cadscene/projects/service.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/srt/test_capability.py`
- Test: `tests/video_analysis/test_recommendation.py`
- Test: `tests/workflow/test_data_import.py`
- Test: `tests/projects/test_models.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: existing SRT coverage fields `gps_coverage`, `rel_alt_coverage`, and `full_pose_coverage`.
- Produces: `recommended_workflow/resolved_workflow == "srt_fixed_track_visual_pose"` for GPS + relative-height SRT without complete gimbal attitude.

- [x] **Step 1: Write failing routing tests**

```python
def test_partial_srt_with_relative_height_routes_to_fixed_track_visual_pose():
    analysis = analyze_srt_capability(PARTIAL_SRT_WITH_REL_ALT)
    assert analysis["detected_mode"] == "srt_fixed_track_visual_pose"

def test_partial_srt_without_relative_height_does_not_use_fixed_track():
    analysis = analyze_srt_capability(SRT_WITH_ABS_ALT_ONLY)
    assert analysis["detected_mode"] == "sfm_only"
    assert "relative height" in " ".join(analysis["warnings"]).lower()

def test_project_selector_exposes_new_workflow_and_hides_legacy_workflow():
    html = Path("apps/project_workspace/index.html").read_text(encoding="utf-8")
    assert '<option value="srt_fixed_track_visual_pose">SRT 轨迹 + 视觉姿态</option>' in html
    assert '<option value="srt_sfm_fused">' not in html
```

- [x] **Step 2: Run the focused tests and verify they fail**

Run: `python -m pytest tests/srt/test_capability.py tests/video_analysis/test_recommendation.py tests/workflow/test_data_import.py tests/viewer/test_project_workspace_static.py -q`

Expected: failures show that partial SRT still resolves to `srt_sfm_fused` and the new selector option is absent.

- [x] **Step 3: Implement the new route without deleting legacy reads**

Use one shared literal in routing branches:

```python
FIXED_TRACK_VISUAL_POSE = "srt_fixed_track_visual_pose"

if trajectory_ready and gimbal_complete and full_pose_coverage >= _MIN_COVERAGE:
    mode = "srt_full_pose"
elif gps_coverage >= _MIN_COVERAGE and rel_alt_coverage >= _MIN_COVERAGE:
    mode = FIXED_TRACK_VISUAL_POSE
else:
    mode = "sfm_only"
```

Add the new value to project-service validation, map partial imports/recommendations to it, add `srt_fixed_track_visual_pose: "SRT 轨迹 + 视觉姿态"` to `WORKFLOW_LABELS`, replace the legacy dropdown option, and retain the legacy adapter registry entry solely so old manifests remain readable.

- [x] **Step 4: Run focused tests**

Run: `python -m pytest tests/srt/test_capability.py tests/video_analysis/test_recommendation.py tests/workflow/test_data_import.py tests/projects/test_models.py tests/viewer/test_project_workspace_static.py -q`

Expected: all selected tests pass and no test expects a new partial-SRT recommendation of `srt_sfm_fused`.

- [x] **Step 5: Commit**

```text
git add cadscene/srt/capability.py cadscene/video_analysis/recommendation.py cadscene/workflow/data_import.py cadscene/projects/service.py apps/project_workspace/index.html apps/project_workspace/project_workspace.js tests/srt/test_capability.py tests/video_analysis/test_recommendation.py tests/workflow/test_data_import.py tests/projects/test_models.py tests/viewer/test_project_workspace_static.py
git commit -m "feat: route partial SRT to fixed-track visual pose"
```

### Task 2: Build the immutable SRT→CAD position track

**Files:**
- Create: `cadscene/srt/fixed_track_visual_pose.py`
- Modify: `cadscene/srt/__init__.py`
- Test: `tests/srt/test_fixed_track_visual_pose.py`

**Interfaces:**
- Consumes: `SrtRecord`, authoritative frame-map payload, `CadGeoreference`, existing `project_wgs84_to_cad_raw`, `cad_raw_to_local_m`, and `horizontal_fov_intrinsics`.
- Produces: `FixedTrackVisualPoseConfig`, `FixedTrackPosition`, `build_fixed_track_positions(...) -> tuple[FixedTrackPosition, ...]`.

- [x] **Step 1: Write failing position-contract tests**

```python
def test_positions_use_projected_xy_relative_alt_and_one_route_offset():
    config = FixedTrackVisualPoseConfig(
        clip_id="clip-1",
        source_start_pts=0,
        source_end_pts_exclusive=3,
        source_time_base=Fraction(1, 1),
        georeference=confirmed_georeference(),
        cad_origin_xy=(500000.0, 3200000.0),
        cad_scale=1.0,
        horizontal_fov_deg=72.0,
        route_offset_xyz_m=(3.0, -2.0, 7.5),
    )
    rows = build_fixed_track_positions(records(), frame_map(), config)
    assert rows[0].center[2] == pytest.approx(records()[0].rel_alt + 7.5)
    assert np.asarray(rows[1].center) - np.asarray(rows[1].canonical_center) == pytest.approx([3.0, -2.0, 7.5])
    assert rows[0].abs_alt == records()[0].abs_alt
    assert rows[0].height_source == "rel_alt"

def test_abs_alt_only_is_rejected_instead_of_becoming_z():
    with pytest.raises(ValueError, match="relative height"):
        build_fixed_track_positions(abs_alt_only_records(), frame_map(), config())
```

- [x] **Step 2: Verify red tests**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py -q`

Expected: import failure for the new module.

- [x] **Step 3: Implement strict config and position records**

```python
@dataclass(frozen=True)
class FixedTrackVisualPoseConfig:
    clip_id: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    georeference: CadGeoreference
    cad_origin_xy: tuple[float, float]
    cad_scale: float
    horizontal_fov_deg: float
    route_offset_xyz_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max_interpolation_gap_sec: float = 1.5
    minimum_position_coverage: float = 0.8
    keyframe_interval_sec: float = 0.5
    max_features: int = 2000
    min_pair_matches: int = 24
    max_orientation_interpolation_gap_sec: float = 2.0

@dataclass(frozen=True)
class FixedTrackPosition:
    frame_index: int
    source_pts: int
    pts_time_sec: float
    canonical_center: tuple[float, float, float]
    center: tuple[float, float, float]
    latitude: float
    longitude: float
    rel_alt: float
    abs_alt: float | None
    interpolated: bool
    height_source: str = "rel_alt"
```

Validate all config numbers, require confirmed georeference, derive XY through existing projection helpers, reject every frame sample without finite `rel_alt`, preserve `abs_alt` only on the audit record, and reject total position coverage below `minimum_position_coverage`.

- [x] **Step 4: Run position tests**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py -q`

Expected: position-contract, offset, axis mapping, bounded interpolation, invalid FOV, and abs-alt rejection tests pass.

- [x] **Step 5: Commit**

```text
git add cadscene/srt/fixed_track_visual_pose.py cadscene/srt/__init__.py tests/srt/test_fixed_track_visual_pose.py
git commit -m "feat: build strict SRT CAD position tracks"
```

### Task 3: Estimate rotations with fixed camera centers

**Files:**
- Modify: `cadscene/srt/fixed_track_visual_pose.py`
- Test: `tests/srt/test_fixed_track_visual_pose.py`

**Interfaces:**
- Consumes: `FixedTrackPosition`, fixed PINHOLE intrinsics, video frames.
- Produces: `PairRotationMeasurement`, `OrientationSolution`, `solve_fixed_center_rotations(...)`, and `estimate_video_orientations(...)`.

- [x] **Step 1: Write failing synthetic geometry tests**

```python
def test_rotation_solver_recovers_world_anchored_rotations_without_changing_centers():
    centers, expected_rotations, measurements = synthetic_curved_track_measurements()
    frozen = centers.copy()
    solution = solve_fixed_center_rotations(centers, measurements)
    np.testing.assert_array_equal(centers, frozen)
    assert solution.status == "orientation_ready"
    assert max(rotation_error_deg(a, b) for a, b in zip(solution.rotations, expected_rotations)) < 1.0

def test_straight_track_reports_position_only_instead_of_guessing_attitude():
    solution = solve_fixed_center_rotations(*synthetic_straight_track())
    assert solution.status == "position_only"
    assert solution.rotations == {}
    assert "unobservable" in " ".join(solution.warnings).lower()
```

- [x] **Step 2: Verify red tests**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py -k "rotation or straight" -q`

Expected: missing solver types/functions.

- [x] **Step 3: Implement pair measurements and world anchoring**

```python
@dataclass(frozen=True)
class PairRotationMeasurement:
    first_frame: int
    second_frame: int
    rotation_second_from_first: np.ndarray
    translation_direction_second: np.ndarray
    inlier_count: int

@dataclass(frozen=True)
class OrientationSolution:
    status: str
    rotations: Mapping[int, np.ndarray]
    diagnostics: tuple[Mapping[str, object], ...]
    warnings: tuple[str, ...]
```

For each contiguous measurement chain, accumulate `A_i0 = R_i R_0ᵀ`. Each pair supplies the fixed-center constraint

```text
normalize(A_j0ᵀ t_j) = R_0 normalize(C_i - C_j)
```

Solve `R_0` with weighted `scipy.spatial.transform.Rotation.align_vectors`, require at least two non-collinear world-baseline directions by singular-value ratio, then recover `R_i=A_i0 R_0`. Reject non-finite matrices, determinant outside tolerance, inconsistent translation directions, and chains shorter than two usable pairs. Do not pass centers to an optimizer or return modified centers.

- [x] **Step 4: Implement OpenCV measurement extraction**

Decode the physical clip sequentially at `round(fps * keyframe_interval_sec)` spacing. Scale frames to at most 960 pixels wide, detect ORB features, match consecutive descriptors with Hamming KNN ratio 0.75, call `cv2.findEssentialMat(..., cv2.RANSAC, 0.999, 1.0)`, then `cv2.recoverPose`. Store only pairs with at least `min_pair_matches` ratio-test matches and at least 15 recover-pose inliers. All frame indexes come from the same authoritative frame-map ordinal.

- [x] **Step 5: Add bounded quaternion interpolation tests and implementation**

```python
def test_orientation_interpolation_never_crosses_large_unobserved_gap():
    poses = interpolate_orientations({0: R0, 10: R10}, frame_times(), max_gap_sec=0.2)
    assert poses[5] is None

def test_orientation_interpolation_uses_slerp_inside_bound():
    poses = interpolate_orientations({0: R0, 10: R10}, frame_times(), max_gap_sec=1.0)
    assert rotation_error_deg(poses[5], expected_half_rotation) < 1e-6
```

Use SciPy `Slerp` only inside each supported interval. Publish no quaternion for orientation-unavailable frames.

- [x] **Step 6: Run core tests and commit**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py -q`

Expected: all fixed-track core tests pass.

```text
git add cadscene/srt/fixed_track_visual_pose.py tests/srt/test_fixed_track_visual_pose.py
git commit -m "feat: estimate visual attitude on fixed SRT centers"
```

### Task 4: Publish an independent CLI and workflow adapter

**Files:**
- Create: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Create: `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`
- Modify: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- Consumes: JSON config written by the adapter, physical MP4/SRT, authoritative frame map.
- Produces: `02_srt_visual_pose/camera_trajectory_visual_pose.json`, `camera_path_srt_locked.csv`, `orientation_diagnostics.json`, `visual_pose_report.md`, plus route-first viewer artifacts.

- [x] **Step 1: Write failing adapter isolation tests**

```python
def test_fixed_track_adapter_never_invokes_sfm_or_legacy_fusion(adapter_inputs):
    adapter = default_workflow_adapters().for_workflow("srt_fixed_track_visual_pose")
    commands = adapter.build_commands(adapter.prepare_inputs(adapter_inputs))
    assert len(commands) == 1
    joined = " ".join(commands[0])
    assert "cadscene.cli.build_srt_fixed_track_visual_pose" in joined
    assert "run_sfm" not in joined
    assert "fuse_srt_sfm" not in joined

def test_new_adapter_has_no_sparse_point_output(adapter_inputs):
    result = adapter.validate_outputs(adapter_inputs)
    assert result.ok
    assert "sparse_points" not in result.outputs
```

- [x] **Step 2: Verify adapter tests fail**

Run: `python -m pytest tests/projects/test_workflow_adapters.py -k fixed_track -q`

Expected: registry has no adapter named `srt_fixed_track_visual_pose`.

- [x] **Step 3: Implement CLI orchestration and progress**

CLI arguments are exactly `--dataset`, `--run-id`, `--output-root`, `--video`, `--srt`, `--frame-map`, `--config`, and `--progress-file`. Emit atomic progress stages with monotonic fractions:

```python
STAGES = (
    ("parse_srt", "正在解析 SRT", 0.08),
    ("project_track", "正在将 SRT 轨迹投影到 CAD", 0.22),
    ("extract_visual_constraints", "正在提取视觉姿态约束", 0.55),
    ("solve_orientation", "正在估计固定轨迹上的相机姿态", 0.82),
    ("publish_workbench", "正在准备轨迹工作台", 0.95),
    ("completed", "SRT 轨迹与视觉姿态已生成", 1.0),
)
```

Trajectory metadata contains `workflow`, `trajectory_mode`, `trajectory_status`, `metric_scale_locked`, `position_source`, `orientation_source`, `height_source`, `absolute_height_usage`, `route_offset_xyz_m`, FOV, georeference, counts, coverage, and warnings. Every pose has a center; only orientation-available poses set `registered=true` and include `cam_from_world_quat_wxyz`.

- [x] **Step 4: Publish route-first viewer artifacts**

Write `03_alignment/camera_track_pred.json` from orientation-available frames and `05_viewer_scene/sfm_viewer_scene.json` with empty points and every position-available frame in `tracks.global_sfm_track`. Convert local CAD metres to web CAD coordinates with existing `cad_meters_to_web_camera`; set `orientation_available=false` on line-only entries so the UI does not draw a frustum for them.

- [x] **Step 5: Register and validate adapter v1**

```python
ExistingWorkflowAdapter(
    name="srt_fixed_track_visual_pose",
    version="1",
    srt_requirement="trajectory",
    modules=("cadscene.cli.build_srt_fixed_track_visual_pose",),
    output_relative_path="02_srt_visual_pose/camera_trajectory_visual_pose.json",
)
```

Write `srt_fixed_track_visual_pose_config.json` inside the immutable attempt and include its hash plus trajectory, diagnostics, CSV, report, frame map, initial camera track, and viewer scene in validation proof.

- [x] **Step 6: Run CLI/adapter tests and commit**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/projects/test_workflow_adapters.py tests/packaging/test_wheel_contents.py -q`

Expected: all selected tests pass; command inspection contains no SfM/fusion module and output tree contains no PLY.

```text
git add cadscene/cli/build_srt_fixed_track_visual_pose.py cadscene/projects/workflow_adapters.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/projects/test_workflow_adapters.py tests/packaging/test_wheel_contents.py
git commit -m "feat: add fixed-track visual pose adapter"
```

### Task 5: Add project configuration, preflight, and immutable fingerprints

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/style.css`
- Create: `tests/projects/test_fixed_track_visual_pose_configuration.py`
- Modify: `tests/cli/test_serve_viewer_project_api.py`

**Interfaces:**
- Consumes: confirmed project `_cad_georeference`, clip SRT capability, physical assets, source interval/frame map.
- Produces: `manual_definition["srt_fixed_track_visual_pose"]`, PATCH endpoint `/api/projects/{project}/clips/{clip}/srt-fixed-track-visual-pose`, and adapter parameters.

- [ ] **Step 1: Write failing service/API tests**

```python
def test_fixed_track_preflight_requires_georeference_fov_and_relative_height(service):
    result = service.preflight_trajectory_jobs("p1", clip_ids=["clip-1"])
    assert result.skipped == ("clip-1",)
    assert "horizontal FOV" in result.reasons["clip-1"]

def test_settings_persist_one_xyz_offset_and_stale_old_trajectory(service):
    manifest = service.update_srt_fixed_track_visual_pose_settings(
        "p1", "clip-1", expected_revision=1,
        horizontal_fov_deg=72.0, route_offset_xyz_m=(2.0, -1.0, 5.0),
    )
    assert manifest.clips[0].manual_definition["srt_fixed_track_visual_pose"]["route_offset_xyz_m"] == [2.0, -1.0, 5.0]
    assert all(ref.value["status"] == "stale" for ref in manifest.clips[0].references)
```

- [ ] **Step 2: Verify red tests**

Run: `python -m pytest tests/projects/test_fixed_track_visual_pose_configuration.py tests/cli/test_serve_viewer_project_api.py -k fixed_track -q`

Expected: missing update method/route and preflight does not enforce the new contract.

- [ ] **Step 3: Implement settings and adapter parameters**

Validate FOV and all three offsets as finite values. Store:

```json
{
  "schema_version": 1,
  "horizontal_fov_deg": 72.0,
  "route_offset_xyz_m": [2.0, -1.0, 5.0]
}
```

Build adapter parameters from current CAD georeference, CAD `origin_xy/cad_scale`, exact source interval/frame map, video metadata, and the saved settings. Include these values in `_trajectory_job_input_payload` and current fingerprint comparison.

- [ ] **Step 4: Generalize the SRT configuration dialog**

Show the existing coordinate/FOV dialog conditionally for both `srt_full_pose` and `srt_fixed_track_visual_pose`. For the new route change the explanatory text to “SRT 提供严格位置和相对高度，视觉算法只估计姿态，不执行三维重建”，show X/Y/Z uniform offset inputs, and save to the new endpoint. Full-pose keeps its existing attitude profile and Z-only behavior.

- [ ] **Step 5: Run configuration tests and commit**

Run: `python -m pytest tests/projects/test_fixed_track_visual_pose_configuration.py tests/projects/test_full_pose_configuration.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_project_workspace_static.py -q`

Expected: new and full-pose settings pass independently, invalid offsets/FOV fail, and stale/fingerprint behavior passes.

```text
git add cadscene/projects/service.py cadscene/projects/http_api.py apps/project_workspace/index.html apps/project_workspace/project_workspace.js apps/project_workspace/style.css tests/projects/test_fixed_track_visual_pose_configuration.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_project_workspace_static.py
git commit -m "feat: configure fixed-track visual pose projects"
```

### Task 6: Start trajectory preparation before entering the workbench

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: new workflow preflight, project job revision, clip-export readiness, active validated trajectory job.
- Produces: HTTP 202 `preparing_trajectory`, visible trajectory-job progress, and HTTP 201 only after trajectory materialization is ready.

- [ ] **Step 1: Write failing workbench-entry tests**

```python
def test_fixed_track_open_queues_trajectory_instead_of_opening_sfm_workbench(api):
    response = api.handle("POST", "/api/projects/p1/clips/c1/workbench-sessions", payload())
    assert response.status == 202
    assert response.body["state"] == "preparing_trajectory"
    assert response.body["job_id"]

def test_fixed_track_workbench_only_opens_with_validated_trajectory(api):
    complete_fixed_track_job(api)
    response = api.handle("POST", "/api/projects/p1/clips/c1/workbench-sessions", payload())
    assert response.status == 201
    assert response.body["launch_mode"] == "trajectory_ready"
    assert "workflowStage=keyframes" in response.body["workbench_url"]
```

- [ ] **Step 2: Verify red tests**

Run: `python -m pytest tests/projects/test_workbench_sessions.py -k fixed_track -q`

Expected: current service creates a `workflow_start` session and returns an SfM-stage URL.

- [ ] **Step 3: Gate fixed-track workbench creation and enqueue trajectory**

For this workflow, `resolve_context.can_open_workbench` requires a current validated trajectory. In `_create_workbench_session`, after physical clip preparation succeeds, call `enqueue_trajectory_jobs` for the selected clip when preflight is eligible; return state `preparing_trajectory`. Never create a fixed-track `workflow_start` session.

- [ ] **Step 4: Show trajectory progress and auto-enter**

Generalize `waitForWorkbenchPreparation` so `preparing_trajectory` reads `clip.status`, `clip.stage`, and `clip.progress`. Display the adapter messages for SRT parse, CAD projection, visual constraints, attitude solve, and workbench publication. On success, call `openWorkbench` again; on failure show the exact job error.

- [ ] **Step 5: Run workbench-entry tests and commit**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py -q`

Expected: existing clip-export behavior passes; fixed-track sessions are never `workflow_start`.

```text
git add cadscene/projects/http_api.py cadscene/projects/workbench_sessions.py apps/project_workspace/project_workspace.js tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py
git commit -m "feat: prepare SRT trajectory before workbench entry"
```

### Task 7: Provide a fixed-track workbench and orientation-only corrections

**Files:**
- Modify: `cadscene/sfm/trajectory.py`
- Modify: `cadscene/alignment/aligner.py`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `cadscene/projects/workbench_sessions.py`
- Test: `tests/alignment/test_aligner.py`
- Test: `tests/sfm/test_trajectory.py`
- Test: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Consumes: trajectory metadata `position_source=srt_cad_locked`, initial camera track, position-line viewer scene, manual camera-track keyframes.
- Produces: a loader with independent position/orientation availability, three-stage fixed-track UI, and alignment output whose positions equal canonical centers plus one uniform translation.

- [ ] **Step 1: Write failing position-only loader and alignment hard-lock tests**

```python
def test_fixed_track_loader_keeps_positions_when_no_orientation_is_available(tmp_path):
    trajectory = load_sfm_trajectory(write_position_only_fixed_track(tmp_path))
    assert trajectory.position_frames.tolist() == [0, 1, 2]
    np.testing.assert_allclose(trajectory.center_at(1), [10.0, 20.0, 80.0])
    assert trajectory.is_orientation_available(1) is False

def test_fixed_track_alignment_accepts_per_frame_attitude_edits_but_only_one_translation(tmp_path):
    result = run_alignment(
        trajectory_path=fixed_track_trajectory(tmp_path),
        web_camera_track_path=attitude_only_keyframes_with_uniform_offset(tmp_path),
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )
    expected = canonical_centers() + np.asarray([2.0, -3.0, 4.0])
    np.testing.assert_allclose(path_centers(result.sfm_camera_path_rows), expected)
    assert result.alignment_json["validation"]["position_source"] == "srt_cad_locked"

def test_fixed_track_alignment_rejects_nonuniform_position_edits(tmp_path):
    with pytest.raises(RuntimeError, match="one whole-route XYZ offset"):
        run_alignment(
            trajectory_path=fixed_track_trajectory(tmp_path),
            web_camera_track_path=keyframes_with_different_position_offsets(tmp_path),
            config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
        )
```

- [ ] **Step 2: Verify red tests**

Run: `python -m pytest tests/sfm/test_trajectory.py tests/alignment/test_aligner.py -k "fixed_track or position_only" -q`

Expected: the loader raises because fewer than two registered poses exist, and fixed-track metadata currently uses the full-pose uniform-angle branch or permits the wrong UI behavior.

- [ ] **Step 3: Separate position availability from orientation availability in the loader**

Extend `SfmTrajectory` with `position_frames` and `position_centers`, plus:

```python
def center_at(self, frame_index: float) -> np.ndarray: ...
def is_orientation_available(self, frame_index: float) -> bool: ...
def orientation_at(self, frame_index: float) -> np.ndarray: ...
```

For ordinary SfM trajectories these arrays mirror registered pose frames/centers. For `position_source=srt_cad_locked`, load every pose with `position_available=true` into the position arrays even when `registered=false` and no quaternion is present. Permit zero orientation poses only for this fixed-track mode with at least two positions. Existing `query` remains strict and raises on missing orientation so callers cannot accidentally render an invented pose.

- [ ] **Step 4: Implement fixed-track alignment mode**

Detect `position_source == "srt_cad_locked"`. Compute all manual-center minus `center_at(frame)` vectors, require their maximum deviation from the median to stay below the existing metric position tolerance, apply only that median translation, and set every positional residual to zero. Treat manual yaw/pitch/roll as absolute orientation anchors when no automatic orientation exists and as corrections where an automatic orientation exists; interpolate rotations with quaternion SLERP, never Euler interpolation across wrap boundaries. Set `position_mode="srt_fixed_track"`, `scale=1`, identity world rotation, and validation fields `metric_scale_locked=true`, `position_source=srt_cad_locked`. Generated path rows use `center_at(frame)+translation` for every position frame and are `unregistered` only where neither automatic nor bounded manual/interpolated orientation is available.

- [ ] **Step 5: Implement the fixed-track UI layout and locks**

Add `isFixedTrackVisualPoseWorkflow()`. Hide upload/SfM and quality steps/panels, relabel keyframes to `视觉姿态`, relabel render to `微调与渲染`, and map `keyframes -> render`. Keep the position-line scene visible with zero points. While video time changes, take x/y/z from the fixed route rather than linearly interpolating sparse manual keyframe positions. Lock x/y/z in camera controls while adding or editing attitude keyframes; backend validation remains authoritative. Do not show frustums for scene entries with `orientation_available=false`.

- [ ] **Step 6: Make resume rules skip SfM and quality**

For `srt_fixed_track_visual_pose`, `_resume_workflow_stage` returns `keyframes` whenever a trajectory is bound and `render` after a saved workbench output. `_validated_resume_stage` maps requested `sfm` to `keyframes` and requested `quality` to `render`; a session without a trajectory is not creatable.

- [ ] **Step 7: Run workbench/alignment tests and commit**

Run: `python -m pytest tests/sfm/test_trajectory.py tests/alignment/test_aligner.py tests/viewer/test_workflow_ui_static.py tests/projects/test_workbench_sessions.py -q`

Expected: fixed positions are exact, nonuniform edits are rejected, and static workflow assertions show no SfM/quality stage for the new route.

```text
git add cadscene/sfm/trajectory.py cadscene/alignment/aligner.py apps/web_camera_viewer/workflow.js apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/index.html apps/web_camera_viewer/style.css cadscene/projects/workbench_sessions.py tests/sfm/test_trajectory.py tests/alignment/test_aligner.py tests/viewer/test_workflow_ui_static.py tests/projects/test_workbench_sessions.py
git commit -m "feat: add fixed-track visual pose workbench"
```

### Task 8: Render without point cloud, run end-to-end verification, and document behavior

**Files:**
- Modify: `cadscene/projects/workbench_render_adapter.py`
- Create: `configs/pipelines/srt_fixed_track_visual_pose_overlay.yaml`
- Modify: `tests/projects/test_render_adapters.py`
- Create: `tests/integration/test_srt_fixed_track_visual_pose_workflow.py`
- Modify: `tests/packaging/test_wheel_contents.py`
- Modify: `docs/current/05-architecture-and-flow.md`
- Modify: `docs/current/09-user-guide.md`

**Interfaces:**
- Consumes: validated fixed-track trajectory, saved workbench camera track, physical video/frame map, CAD dataset.
- Produces: packaged overlay video without sparse-point inputs, benchmark timing evidence, and updated operator documentation.

- [ ] **Step 1: Write failing render isolation and integration tests**

```python
def test_fixed_track_render_plan_has_no_sparse_ply_and_no_quality_stage(render_inputs):
    plan = adapter_for("srt_fixed_track_visual_pose").prepare(render_inputs)
    joined = " ".join(plan.commands[0])
    assert "--sparse-ply" not in joined
    assert "alignment,render" in joined
    assert "quality" not in joined

def test_fixed_track_end_to_end_exports_track_without_point_cloud(tmp_path):
    result = run_fixture_project(tmp_path)
    assert result.trajectory["meta"]["position_source"] == "srt_cad_locked"
    assert result.viewer_scene["points"]["count_exported"] == 0
    assert result.viewer_scene["tracks"]["global_sfm_track"]
    assert not list(result.run_root.rglob("*.ply"))
```

- [ ] **Step 2: Verify red tests**

Run: `python -m pytest tests/projects/test_render_adapters.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py -k fixed_track -q`

Expected: no render adapter/config exists for the new workflow.

- [ ] **Step 3: Add no-point render adapter and pipeline**

Register the new workflow in `default_workbench_render_adapters`. Select `srt_fixed_track_visual_pose_overlay.yaml`, call `run_pipeline --stages alignment,render`, and place no `--sparse-ply` argument. The pipeline contains only `alignment`, optional zero-point `viewer_scene`, and `render`; no `quality` or `road_surface` stage.

- [ ] **Step 4: Add timing comparison and docs**

Record core CLI phase timings in diagnostics/report. In the integration smoke, compare reported fixed-track processing time with the same fixture’s SfM command plan without claiming a universal ratio. Document that speed comes from skipping position registration, triangulation, bundle adjustment, and point-cloud maintenance—not merely skipping PLY serialization.

- [ ] **Step 5: Run focused and full verification**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/projects/test_fixed_track_visual_pose_configuration.py tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/projects/test_render_adapters.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

Expected: all focused tests pass.

Run: `python -m pytest -q`

Expected: full suite passes with only the repository’s pre-existing documented skips.

- [ ] **Step 6: Run the real local smoke and inspect artifacts**

Use project `p-2962f7bb456144d5` under the isolated UI-test storage root. Confirm the 12017-record SRT produces a CAD-contained trajectory, position Z preserves the observed `rel_alt` range around 84.926–91.846 metres before uniform offset, the workbench shows the route even if attitude coverage is partial, and the attempt contains no PLY.

- [ ] **Step 7: Commit**

```text
git add cadscene/projects/workbench_render_adapter.py configs/pipelines/srt_fixed_track_visual_pose_overlay.yaml tests/projects/test_render_adapters.py tests/integration/test_srt_fixed_track_visual_pose_workflow.py tests/packaging/test_wheel_contents.py docs/current/05-architecture-and-flow.md docs/current/09-user-guide.md
git commit -m "feat: complete fixed-track visual pose workflow"
```

### Task 9: Restart the isolated service for user acceptance

**Files:**
- No repository files are modified.

**Interfaces:**
- Consumes: completed branch, UI-test storage root, port 8310.
- Produces: live project-library URL for manual upload/acceptance.

- [ ] **Step 1: Stop only the previously identified port-8310 service process**

Resolve the listening PID for `127.0.0.1:8310`, verify its command line belongs to this worktree/service, and stop only that PID.

- [ ] **Step 2: Start the service from the isolated worktree**

Run the existing viewer service command with storage root `D:\zjic2026\cadscene_workbench\work\srt-full-pose-ui-test` and bind `127.0.0.1:8310`.

Expected: the process remains running and `/apps/project_library/` returns HTTP 200.

- [ ] **Step 3: Open the project-library page and hand off acceptance steps**

Open `http://127.0.0.1:8310/apps/project_library/`. Ask the user to create/upload the three-file project, confirm the CAD candidate and FOV, click `进入工作台`, and verify the progress sequence and CAD-contained trajectory.
