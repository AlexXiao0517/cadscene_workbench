# SRT Whole-Video Pose Anchor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep SRT-backed uploads as one whole-video scene and recover absolute fixed-track attitude from one user-confirmed recommended visual anchor.

**Architecture:** Video analysis selects a single authoritative PTS interval whenever an SRT asset is present, leaving non-SRT segmentation unchanged. The visual solver publishes both absolute rotations and gauge-free per-component relative rotations; fixed-track alignment applies one or more manual world-orientation anchors without changing SRT camera centers. The workbench exposes the recommendation and uses the existing durable camera-track save path.

**Tech Stack:** Python 3, NumPy, SciPy Rotation/Slerp, OpenCV, pytest, vanilla JavaScript, Three.js.

## Global Constraints

- SRT-backed analysis produces exactly one logical clip over the complete decoded-frame PTS range.
- SRT→CAD XYZ remains immutable except for one whole-route XYZ offset.
- Visual processing estimates orientation only and produces no point cloud.
- A single confirmed attitude anchor is sufficient for a connected relative-orientation component.
- Fixed-track workbench never invokes SfM or quality stages.

---

### Task 1: Preserve SRT uploads as one logical clip

**Files:**
- Modify: `cadscene/video_analysis/segmentation.py`
- Modify: `cadscene/video_analysis/analyzer.py`
- Test: `tests/video_analysis/test_segmentation.py`
- Test: `tests/video_analysis/test_analyzer.py`

**Interfaces:**
- Produces: `plan_single_source_interval(frame_index: DecodedFrameIndex) -> list[PlannedClip]`
- Consumes: decoded frame index already built by `analyze_video`

- [ ] **Step 1: Write the failing unit tests**

```python
def test_single_source_interval_covers_every_decoded_frame_once():
    index = decoded_index_with_duration(200.5)
    clips = plan_single_source_interval(index)
    assert len(clips) == 1
    assert clips[0].source_start_pts == index.source_start_pts
    assert clips[0].source_end_pts_exclusive == index.source_end_pts_exclusive

def test_analyzer_keeps_long_srt_upload_as_one_clip(tmp_path):
    manifest = run_analysis_fixture(tmp_path, duration_sec=200.5, include_srt=True)
    assert len(manifest["clips"]) == 1
    assert manifest["clips"][0]["source_start_pts_sec"] == 0.0
    assert manifest["clips"][0]["source_end_pts_sec"] == 200.5
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/video_analysis/test_segmentation.py tests/video_analysis/test_analyzer.py -q`

Expected: failure because `plan_single_source_interval` is missing and long SRT uploads still use duration cuts.

- [ ] **Step 3: Implement the single-interval planner and analyzer branch**

```python
def plan_single_source_interval(frame_index: DecodedFrameIndex) -> list[PlannedClip]:
    clip = PlannedClip(
        start_pts_sec=frame_index.source_start_pts_sec,
        end_pts_sec=frame_index.source_end_pts_exclusive_sec,
        start_boundary=BoundaryEvidence(frame_index.source_start_pts_sec, ("source_start",), 1.0),
        end_boundary=BoundaryEvidence(frame_index.source_end_pts_exclusive_sec, ("source_end",), 1.0),
        needs_review=False,
        source_start_pts=frame_index.source_start_pts,
        source_end_pts_exclusive=frame_index.source_end_pts_exclusive,
        source_time_base=frame_index.time_base,
    )
    validate_frame_partition(frame_index, [clip])
    return [clip]
```

In `analyze_video`, select this planner when `srt_path is not None`; otherwise preserve the existing `plan_clip_intervals` call.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/video_analysis/test_segmentation.py tests/video_analysis/test_analyzer.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add cadscene/video_analysis/segmentation.py cadscene/video_analysis/analyzer.py tests/video_analysis/test_segmentation.py tests/video_analysis/test_analyzer.py
git commit -m "feat: keep SRT video as one scene"
```

### Task 2: Publish gauge-free visual attitude and an anchor recommendation

**Files:**
- Modify: `cadscene/srt/fixed_track_visual_pose.py`
- Modify: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Modify: `cadscene/sfm/trajectory.py`
- Test: `tests/srt/test_fixed_track_visual_pose.py`
- Test: `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`
- Test: `tests/sfm/test_trajectory.py`

**Interfaces:**
- Produces: `OrientationSolution.relative_rotations`, `component_ids`, `recommended_anchor_frame`
- Produces trajectory pose fields `visual_component_id` and `cam_from_visual_local_quat_wxyz`
- Produces `SfmTrajectory.relative_orientation_at(frame_index)` and `relative_component_at(frame_index)`

- [ ] **Step 1: Write failing solver and schema tests**

```python
def test_straight_track_keeps_relative_rotations_when_world_anchor_is_unobservable():
    solved = solve_fixed_center_rotations(straight_centers(), connected_measurements())
    assert solved.status == "position_only"
    assert solved.rotations == {}
    assert len(solved.relative_rotations) == 3
    assert solved.recommended_anchor_frame in solved.relative_rotations

def test_loader_reads_relative_visual_orientation_without_marking_it_absolute(tmp_path):
    trajectory = load_sfm_trajectory(write_relative_fixed_track(tmp_path))
    assert not trajectory.is_orientation_available(10)
    assert trajectory.relative_component_at(10) == 0
    np.testing.assert_allclose(trajectory.relative_orientation_at(10), np.eye(3))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/sfm/test_trajectory.py -q`

Expected: missing relative-orientation fields and loader methods.

- [ ] **Step 3: Preserve connected-component transforms and select the recommendation**

For every visual component, retain the `_measurement_components` transforms as `camera_from_visual_local`. Assign stable component IDs ordered by first frame. Select the frame in the largest component with the greatest sum of incident accepted-pair inliers, preferring the earlier frame on ties. Interpolate relative rotations only between known rotations in the same component and within `max_orientation_interpolation_gap_sec`.

Extend the trajectory and viewer-scene payloads with the fields defined in the design spec. Absolute `orientation_available` remains false for relative-only poses.

- [ ] **Step 4: Extend `SfmTrajectory` without changing legacy behavior**

Parse relative quaternions into separate arrays. `relative_orientation_at` must reject frames outside relative coverage or across components. Existing absolute `query` and `orientation_at` semantics remain unchanged.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `python -m pytest tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/sfm/test_trajectory.py -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add cadscene/srt/fixed_track_visual_pose.py cadscene/cli/build_srt_fixed_track_visual_pose.py cadscene/sfm/trajectory.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/sfm/test_trajectory.py
git commit -m "feat: preserve relative fixed-track attitude"
```

### Task 3: Apply one or more fixed-track attitude anchors

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Test: `tests/alignment/test_aligner.py`

**Interfaces:**
- Consumes: `SfmTrajectory.relative_orientation_at` and manual keyframe `CameraState`
- Produces: an `AnchoredAlignment` whose orientation series covers every calibrated visual component and whose positions equal SRT centers plus one uniform offset

- [ ] **Step 1: Write failing one-anchor and drift tests**

```python
def test_fixed_track_one_anchor_propagates_relative_orientation_and_keeps_xyz():
    result = run_alignment_with_relative_track(anchor_frames=[10])
    assert all(row["orientation_available"] for row in result.sfm_camera_path_rows)
    np.testing.assert_allclose(path_centers(result), source_centers())

def test_fixed_track_second_anchor_slerps_correction_when_residual_exceeds_five_degrees():
    result = run_alignment_with_relative_track(anchor_frames=[0, 30], end_yaw_delta=12)
    assert result.alignment_json["validation"]["drift_corrected"] is True
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest tests/alignment/test_aligner.py -q`

Expected: one anchor only affects the exact frame or relative orientations are unavailable.

- [ ] **Step 3: Implement component calibration**

For each relative component with manual anchors, calculate the world-from-local correction at every anchor. Use one constant correction when only one anchor exists or all residuals are at most 5°. Otherwise SLERP correction rotations between anchors and clamp to the nearest endpoint outside them. Apply the correction to each relative orientation while leaving centers unchanged.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/alignment/test_aligner.py -q`

Expected: all alignment tests pass.

- [ ] **Step 5: Commit**

```bash
git add cadscene/alignment/aligner.py tests/alignment/test_aligner.py
git commit -m "feat: calibrate fixed-track attitude from one anchor"
```

### Task 4: Add the fixed-track anchor workflow UI

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes viewer-scene recommendation metadata
- Produces dedicated actions `fixedTrackGoRecommendedAnchor`, `fixedTrackConfirmAnchor`, and one-anchor alignment readiness

- [ ] **Step 1: Write failing static behavior tests**

```python
def test_fixed_track_workbench_has_recommended_anchor_controls():
    assert 'id="fixedTrackAnchorPanel"' in html
    assert 'id="fixedTrackGoRecommendedAnchor"' in html
    assert 'id="fixedTrackConfirmAnchor"' in html

def test_fixed_track_alignment_requires_one_manual_anchor_not_two():
    fixed_branch = script[script.index("async function startAlignmentStage"):]
    assert "requiredAnchorCount = isFixedTrackVisualPoseWorkflow() ? 1 : 2" in fixed_branch
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: missing panel and old two-anchor condition.

- [ ] **Step 3: Implement the panel and event wiring**

Expose read-only fixed-track metadata and a frame-seek function from `viewer_legacy.js`. Show the panel only for `srt_fixed_track_visual_pose`. “跳到推荐帧” seeks with the existing authoritative PTS/frame mapping. “确认姿态锚点” invokes the existing add/update-keyframe action and durable save path. Disable final application until one confirmed anchor belongs to relative coverage.

Update status copy so a missing absolute orientation says “位置可用，姿态待锚定”; never request or mention `02_sfm` in the fixed branch. Add “回到轨迹起点” beside the recommendation action.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add apps/web_camera_viewer/index.html apps/web_camera_viewer/style.css apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/workflow.js tests/viewer/test_workflow_ui_static.py
git commit -m "feat: guide fixed-track attitude anchoring"
```

### Task 5: Integrate, regress, and verify the real SRT sample

**Files:**
- Modify: `tests/integration/test_srt_fixed_track_visual_pose_workflow.py`
- Modify: `apps/web_camera_viewer/index.html` only if cache-busting revision must change

**Interfaces:**
- Verifies all prior task outputs together

- [ ] **Step 1: Add the failing integration assertion**

```python
def test_relative_only_fixed_track_becomes_renderable_after_one_anchor(...):
    assert output["meta"]["position_source"] == "srt_cad_locked"
    assert output["meta"]["point_cloud_generated"] is False
    assert anchored["validation"]["manual_anchor_count"] == 1
```

- [ ] **Step 2: Run the integration test and verify RED, then complete only missing wiring**

Run: `python -m pytest tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

- [ ] **Step 3: Run regression suites**

Run: `python -m pytest tests/video_analysis tests/srt tests/sfm tests/alignment tests/projects tests/viewer tests/integration/test_srt_fixed_track_visual_pose_workflow.py -q`

Expected: all selected tests pass.

- [ ] **Step 4: Re-run analysis and trajectory generation against the uploaded 200.483617-second DJI sample**

Expected evidence: one clip from source PTS 0 to 12029017, 12017 SRT-backed positions, no PLY output, relative visual coverage and a recommended anchor when absolute auto-anchoring remains unavailable.

- [ ] **Step 5: Start the local service and manually verify**

Open the project library, enter the single scene, jump to the recommended anchor, adjust yaw/pitch/roll, confirm once, apply, scrub the video, and verify the SRT marker remains synchronized while the frustum follows the recovered attitude.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_srt_fixed_track_visual_pose_workflow.py apps/web_camera_viewer/index.html
git commit -m "test: verify whole-video fixed-track anchoring"
```

