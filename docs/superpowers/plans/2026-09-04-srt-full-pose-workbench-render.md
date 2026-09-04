# SRT Full-Pose Workbench and Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a complete-pose SRT project open a usable trajectory/frustum workbench, permit only a uniform XYZ route offset, save that adjustment, and enter rendering without SfM, keyframe planning, or quality inspection.

**Architecture:** Extend the existing full-pose trajectory build to publish the viewer's standard initial camera-track and scene artifacts beside the trajectory, so the unchanged loader can show the route and current camera frustum immediately. Add a dedicated two-stage full-pose UI (`轨迹微调 → 渲染导出`) whose only positional edit is an absolute, reversible uniform XYZ offset applied to every SRT pose. Keep rendering on the existing project render adapter, which consumes the saved workbench track and runs only `alignment,render`.

**Tech Stack:** Python 3, pytest, NumPy, browser JavaScript, static HTML/CSS, existing Cadscene project/workbench APIs and pipeline runner.

## Global Constraints

- Complete-pose SRT remains the sole source of per-frame position and attitude; no SfM or visual pose solve is allowed in this route.
- Position editing is limited to one uniform XYZ translation applied to the entire route.
- The horizontal FOV remains the user-supplied project value and is preserved in every generated camera entry.
- The full-pose workbench has exactly two visible workflow stages: trajectory adjustment and render export.
- Quality inspection, keyframe plans, manual pose anchors, sparse point clouds, and individual camera transforms are unavailable in this route.
- Rendering remains a separate user action and uses the saved workbench artifact.
- Existing `sfm_only`, `srt_sfm_fused`, `srt_fixed_track_visual_pose`, and `pure_rotation` behavior must remain compatible.

---

## File Structure

- `cadscene/srt/full_pose_workbench.py`: convert a validated full-pose trajectory into the existing web camera-track and viewer-scene contracts.
- `cadscene/cli/build_srt_full_pose.py`: atomically publish full-pose trajectory, initial track, and viewer scene as one adapter result.
- `cadscene/projects/workflow_adapters.py`: validate and fingerprint the new initial workbench artifacts.
- `apps/web_camera_viewer/full_pose_adjustment.js`: pure functions for uniform route-offset state and camera-track translation.
- `apps/web_camera_viewer/viewer_legacy.js`: expose the minimal live-viewer API that applies an absolute uniform route offset and refreshes track/frustum state.
- `apps/web_camera_viewer/index.html`: provide the full-pose adjustment controls and load their script.
- `apps/web_camera_viewer/workflow.js`: select the full-pose two-stage layout, wire adjustment/save controls, and bypass alignment/quality UI actions.
- `apps/web_camera_viewer/styles.css`: style the focused full-pose adjustment card.
- `cadscene/projects/workbench_sessions.py`: normalize full-pose resume stages to adjustment or render only.
- `cadscene/workflow/job_runner.py`: explicitly reject the quality stage for full-pose runs.
- Focused tests under `tests/srt`, `tests/cli`, `tests/projects`, `tests/workflow`, and `tests/viewer` lock each contract before implementation.

### Task 1: Publish Initial Full-Pose Workbench Artifacts

**Files:**
- Create: `cadscene/srt/full_pose_workbench.py`
- Modify: `cadscene/cli/build_srt_full_pose.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Create: `tests/srt/test_full_pose_workbench.py`
- Modify: `tests/cli/test_build_srt_full_pose_cli.py`
- Modify: `tests/projects/test_workflow_adapters.py`

**Interfaces:**
- Consumes: `build_full_pose_workbench_payloads(trajectory: Mapping[str, object]) -> FullPoseWorkbenchPayloads` and `publish_full_pose_workbench_payloads(run_root: Path, payloads: FullPoseWorkbenchPayloads) -> tuple[Path, Path]`.
- Produces: `03_alignment/camera_track_pred.json` and `05_viewer_scene/sfm_viewer_scene.json`, both expressed in `web_cad_world`, with every registered SRT pose represented and `point_cloud_generated: false`.

- [ ] **Step 1: Write failing payload-contract tests**

```python
def test_full_pose_workbench_payload_preserves_positions_attitudes_and_fov():
    payloads = build_full_pose_workbench_payloads(_trajectory())
    assert [row["frame"] for row in payloads.camera_track["keyframes"]] == [0, 1]
    assert payloads.camera_track["keyframes"][0]["camera"] == {
        "x": 100.0, "y": 200.0, "z": 30.0,
        "yaw": 12.0, "pitch": -35.0, "roll": 2.0, "fov": 59.109,
    }
    assert payloads.camera_track["meta"]["position_edit_policy"] == "uniform_xyz_offset_only"
    assert payloads.viewer_scene["points"]["count_exported"] == 0
    assert payloads.viewer_scene["meta"]["workflow"] == "srt_full_pose"
```

- [ ] **Step 2: Run the payload test and verify RED**

Run: `python -m pytest tests/srt/test_full_pose_workbench.py -q`

Expected: collection fails because `cadscene.srt.full_pose_workbench` does not exist.

- [ ] **Step 3: Implement quaternion-to-web-track conversion**

Implement `FullPoseWorkbenchPayloads`, validate `meta.trajectory_mode == "srt_full_pose"`, skip unregistered poses, decompose `cam_from_world_quat_wxyz` with the repository camera convention, convert CAD metres through `python_state_to_web_camera`, and build:

```python
camera_track = {
    "schema_version": "cadscene_camera_track_pred_v1",
    "fps": fps,
    "keyframes": keyframes,
    "meta": {
        "generated_by": "cadscene.build_srt_full_pose",
        "workflow": "srt_full_pose",
        "position_source": "srt_full_pose",
        "attitude_source": "srt_full_pose",
        "position_edit_policy": "uniform_xyz_offset_only",
    },
}
```

and a `cadscene_sfm_viewer_scene_v1` payload with an empty point set and `tracks.global_sfm_track` containing the same web cameras.

- [ ] **Step 4: Run the payload tests and verify GREEN**

Run: `python -m pytest tests/srt/test_full_pose_workbench.py -q`

Expected: all tests pass.

- [ ] **Step 5: Write failing CLI and adapter validation tests**

Extend the CLI test to assert both new files are published and extend the adapter test to delete each file in turn and assert validation fails with `full-pose artifact output is missing`.

- [ ] **Step 6: Run CLI/adapter tests and verify RED**

Run: `python -m pytest tests/cli/test_build_srt_full_pose_cli.py tests/projects/test_workflow_adapters.py -q`

Expected: failures show the full-pose CLI and adapter do not yet publish/require the viewer artifacts.

- [ ] **Step 7: Atomically publish and fingerprint the artifacts**

Build the payloads from the staged trajectory, write them under staged `03_alignment` and `05_viewer_scene`, then atomically move all three stage directories. Add `initial_camera_track` and `viewer_scene` to full-pose adapter outputs and validation proof hashes.

- [ ] **Step 8: Run focused tests and commit**

Run: `python -m pytest tests/srt/test_full_pose_workbench.py tests/cli/test_build_srt_full_pose_cli.py tests/projects/test_workflow_adapters.py -q`

Expected: all tests pass.

```bash
git add cadscene/srt/full_pose_workbench.py cadscene/cli/build_srt_full_pose.py cadscene/projects/workflow_adapters.py tests/srt/test_full_pose_workbench.py tests/cli/test_build_srt_full_pose_cli.py tests/projects/test_workflow_adapters.py
git commit -m "feat: publish full-pose workbench artifacts"
```

### Task 2: Add Uniform Full-Route XYZ Adjustment

**Files:**
- Create: `apps/web_camera_viewer/full_pose_adjustment.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/styles.css`
- Create: `tests/viewer/test_full_pose_adjustment_node.py`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: a standard camera-track payload and an absolute offset object `{x, y, z}`.
- Produces: `window.CadsceneFullPoseAdjustment.applyAbsoluteOffset(track, previousOffset, nextOffset)` and live APIs `cadsceneApplyFullPoseRouteOffset`, `cadsceneGetFullPoseRouteOffset`, and `cadsceneResetFullPoseRouteOffset`.

- [ ] **Step 1: Write failing Node behavior tests**

```javascript
const shifted = api.applyAbsoluteOffset(track, {x: 1, y: 2, z: 3}, {x: 4, y: 6, z: 8});
assert.deepStrictEqual(shifted.track.keyframes[0].camera, {...camera, x: camera.x + 3, y: camera.y + 4, z: camera.z + 5});
assert.deepStrictEqual(shifted.offset, {x: 4, y: 6, z: 8});
assert.deepStrictEqual(track, original); // pure function
```

Also assert non-finite offsets are rejected and non-position camera fields are byte-for-byte preserved.

- [ ] **Step 2: Run the Node wrapper and verify RED**

Run: `python -m pytest tests/viewer/test_full_pose_adjustment_node.py -q`

Expected: failure because `full_pose_adjustment.js` does not exist.

- [ ] **Step 3: Implement the pure absolute-offset helper**

Use UMD style consistent with the other viewer helpers. Compute `delta = nextOffset - previousOffset`, deep-clone the track, translate only every keyframe camera's `x/y/z`, and store the normalized finite next offset.

- [ ] **Step 4: Run Node tests and verify GREEN**

Run: `python -m pytest tests/viewer/test_full_pose_adjustment_node.py -q`

Expected: all tests pass.

- [ ] **Step 5: Write failing static integration assertions**

Assert `index.html` loads `full_pose_adjustment.js`, contains `#fullPoseAdjustmentPanel`, numeric `#fullPoseOffsetX/Y/Z`, apply/reset buttons, and that `viewer_legacy.js` exposes the three live APIs and calls `syncSfmAnchoredTrackFromCurrentTrack()` after applying an offset.

- [ ] **Step 6: Run static tests and verify RED**

Run: `python -m pytest tests/viewer/test_project_workspace_static.py -q`

Expected: failures identify the missing controls and live APIs.

- [ ] **Step 7: Implement live offset application and focused controls**

Load the helper before `viewer_legacy.js`. In `cadsceneApplyFullPoseRouteOffset`, replace `cameraTrack` with the helper result, update the current interpolated camera, refresh controls, synchronize the anchored path, mark the draft dirty, and redraw both panes. The reset API applies `{x: 0, y: 0, z: 0}`. Add a compact panel explaining that SRT position/attitude are locked and only a route-wide XYZ translation is changed.

- [ ] **Step 8: Run viewer tests and commit**

Run: `python -m pytest tests/viewer/test_full_pose_adjustment_node.py tests/viewer/test_project_workspace_static.py -q`

Expected: all tests pass.

```bash
git add apps/web_camera_viewer/full_pose_adjustment.js apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/index.html apps/web_camera_viewer/styles.css tests/viewer/test_full_pose_adjustment_node.py tests/viewer/test_project_workspace_static.py
git commit -m "feat: add full-pose route offset controls"
```

### Task 3: Replace the SfM Workflow UI with Full-Pose Adjustment and Render

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: the live viewer route-offset APIs and existing `saveCurrentCameraTrack`, `finalizeProjectWorkbenchSave`, `startRenderStage`, and workflow-stage rendering functions.
- Produces: full-pose-only workflow layout and `finishFullPoseAdjustment()`.

- [ ] **Step 1: Write failing static workflow tests**

Assert the full-pose branch:

```javascript
stageTitles.keyframes = "轨迹微调";
stageTitles.render = "渲染导出";
```

hides upload/SfM/quality steps and panels, shows `#fullPoseAdjustmentPanel`, hides `#cameraSettingsDetails` and standard keyframe actions, labels ordinals `1` and `2`, and maps `keyframes -> render` without quality. Assert `finishFullPoseAdjustment()` saves/finalizes the project workbench output and then selects render, rather than running alignment.

- [ ] **Step 2: Run the static workflow test and verify RED**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: failures show full-pose still inherits SfM/keyframe/quality UI behavior.

- [ ] **Step 3: Implement the two-stage layout and control wiring**

Refactor the layout function to derive `full = mode === "srt_full_pose"`. For full pose, hide upload/SfM/quality, use `轨迹微调` and `渲染导出`, show only the full-pose adjustment panel, and keep annotation/render controls available at render. Wire apply/reset buttons to the live viewer APIs, update a cumulative-offset label, and persist the track. Route the existing finish button to `finishFullPoseAdjustment()`.

- [ ] **Step 4: Make stage transitions explicit**

Add a full-pose `nextStageAfterSuccess` table with `sfm -> keyframes`, `keyframes -> render`, `alignment -> render`, and no quality transition. Make `startQualityStage()` throw `SRT 全姿态工作流不包含质量检测阶段` if invoked defensively.

- [ ] **Step 5: Run viewer workflow regressions and commit**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py tests/viewer/test_project_workspace_static.py tests/viewer/test_full_pose_adjustment_node.py -q`

Expected: all tests pass, including existing fixed-track and pure-rotation assertions.

```bash
git add apps/web_camera_viewer/workflow.js tests/viewer/test_workflow_ui_static.py
git commit -m "feat: add full-pose adjustment workflow"
```

### Task 4: Enforce Full-Pose Resume and Backend Stage Rules

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/workflow/test_job_runner.py`

**Interfaces:**
- Consumes: session workflow `srt_full_pose`, trajectory-ready revision/fingerprint, and saved workbench output state.
- Produces: resume stage `keyframes` before adjustment save, `render` after save, and a backend error for any full-pose quality-stage request.

- [ ] **Step 1: Write failing resume-state tests**

Create a trajectory-ready full-pose session and assert requested `quality` normalizes to `keyframes` before a saved workbench output, then to `render` after a valid save. Assert stale resume data cannot restore `quality`.

- [ ] **Step 2: Write failing job-runner test**

```python
with pytest.raises(ValueError, match="full-pose workflow has no quality stage"):
    build_stage_command(root, dataset, run_id, "quality")
```

- [ ] **Step 3: Run both test groups and verify RED**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/workflow/test_job_runner.py -q`

Expected: full-pose currently resumes into quality and the job runner accepts a quality command.

- [ ] **Step 4: Implement explicit full-pose normalization and rejection**

In `_validated_resume_stage`, treat full pose like a two-stage workflow: return `keyframes` until both workbench output revision and fingerprint exist, otherwise `render`. In the job runner, reject `quality` for both full-pose and fixed-track workflows before artifact preflight.

- [ ] **Step 5: Run backend tests and commit**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/workflow/test_job_runner.py -q`

Expected: all tests pass.

```bash
git add cadscene/projects/workbench_sessions.py cadscene/workflow/job_runner.py tests/projects/test_workbench_sessions.py tests/workflow/test_job_runner.py
git commit -m "fix: enforce full-pose workflow stages"
```

### Task 5: Verify the Complete-Pose Project Lifecycle and Start the Service

**Files:**
- Modify only if a failing integration test reveals a defect: `tests/integration/test_srt_full_pose_workflow.py`, `tests/projects/test_render_adapters.py`

**Interfaces:**
- Consumes: merged SRT path, existing project CAD/video inputs, confirmed georeference, and FOV `59.109`.
- Produces: evidence that trajectory construction publishes preview artifacts, workbench save is renderable, and the project-library service is reachable at port `8310`.

- [ ] **Step 1: Run focused full-pose and render integration tests**

Run: `python -m pytest tests/integration/test_srt_full_pose_workflow.py tests/projects/test_render_adapters.py tests/projects/test_executor.py tests/projects/test_render_jobs.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run all affected regression suites**

Run: `python -m pytest tests/srt tests/cli tests/projects tests/workflow tests/viewer -q`

Expected: all tests pass; any pre-existing Windows timing flake must be rerun alone and reported, never hidden.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m pytest -q`

Expected: all tests pass, allowing only documented pre-existing non-reproducible timing failures that pass immediately in isolation.

- [ ] **Step 4: Validate the merged SRT through the real classifier**

Run the existing SRT inspection entry point against `D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.SRT` and confirm `srt_full_pose`, 12,017 records, and 100% yaw/pitch/roll coverage.

- [ ] **Step 5: Start and probe the project-library service**

Start the repository's project-library server on `127.0.0.1:8310` in a hidden background process, then request `/apps/project_library/` and assert HTTP 200. Keep the process alive for user testing.

- [ ] **Step 6: Present the test artifact and UI test procedure**

Give the user the clickable merged SRT path and the project-library URL. Instruct them to create a new project with the merged SRT, video, and CAD, set FOV `59.109`, confirm the CAD coordinate candidate, enter the workbench, verify route/frustum visibility, apply a uniform XYZ offset if needed, save adjustment, then start render.

