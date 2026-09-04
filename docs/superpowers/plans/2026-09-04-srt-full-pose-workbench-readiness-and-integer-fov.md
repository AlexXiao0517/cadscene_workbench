# SRT Full-Pose Workbench Readiness and Integer FOV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure an `srt_full_pose` clip enters the dedicated trajectory-adjustment workbench only after its validated trajectory artifacts exist, shows the live route and camera attitude during playback, and uses an integer horizontal FOV throughout configuration and processing.

**Architecture:** Treat both SRT trajectory workflows as trajectory-required workbench modes in the project/session layer. The project API prepares the trajectory before issuing a workbench session, while the workbench UI defensively maps full-pose sessions to `keyframes` and never to `sfm`. Reuse the authoritative full-pose camera track and viewer scene already emitted by the workflow adapter; add a read-only live-pose panel that reads the interpolated camera on every displayed frame.

**Tech Stack:** Python 3, pytest, vanilla JavaScript, HTML/CSS, Three.js viewer, JSON project repositories.

## Global Constraints

- Work in `D:/zjic2026/cadscene_workbench/.worktrees/srt-full-pose-cad-georeference` on branch `codex/srt-full-pose-cad-georeference`.
- `srt_full_pose` must never start, advertise, or fall back to SfM.
- A full-pose workbench session requires a current successful trajectory job whose published output still matches the current input fingerprint.
- The user may adjust only one uniform XYZ offset; per-frame SRT position, attitude, and FOV stay authoritative.
- Horizontal FOV is an integer from `2` through `178`; historical decimals are rounded to the nearest integer at display and persistence boundaries.
- Full-pose workflow remains two stages: `轨迹微调` then `渲染导出`; it has no quality stage.
- Preserve unrelated dirty-worktree changes and stage only files changed by this plan.

---

## File Structure

- `cadscene/projects/workbench_sessions.py`: defines which workflows require validated trajectory artifacts before a session can open and validates resume stages.
- `cadscene/projects/http_api.py`: queues missing SRT trajectory work before creating a session and returns workflow-specific preparation copy.
- `cadscene/projects/service.py`: normalizes horizontal FOV to the integer contract for both SRT workflows.
- `apps/project_workspace/index.html`: exposes an integer FOV control.
- `apps/project_workspace/project_workspace.js`: rounds historical/displayed FOV, submits an integer, and labels full-pose preparation accurately.
- `apps/web_camera_viewer/index.html`: contains the full-pose live pose readout.
- `apps/web_camera_viewer/style.css`: formats the compact live pose readout.
- `apps/web_camera_viewer/workflow.js`: prevents full-pose SfM fallback and selects the trajectory-adjustment stage.
- `apps/web_camera_viewer/viewer_legacy.js`: updates live position, attitude, and FOV from the interpolated camera on playback/seek.
- `tests/projects/test_workbench_sessions.py`: covers readiness, automatic trajectory queuing, and valid trajectory-ready sessions.
- `tests/projects/test_full_pose_configuration.py`: covers backend integer FOV normalization and adapter propagation.
- `tests/viewer/test_project_workspace_static.py`: covers integer FOV and full-pose preparation UI contracts.
- `tests/viewer/test_workflow_ui_static.py`: covers full-pose stage selection, no-SfM behavior, and live-pose wiring.

---

### Task 1: Require a Validated Full-Pose Trajectory Before Opening the Workbench

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Consumes: `WorkbenchContext.workflow`, `trajectory_job_id`, `trajectory_run_id`, `trajectory_output_revision`, `trajectory_output_fingerprint`.
- Produces: `TRAJECTORY_REQUIRED_WORKBENCH_WORKFLOWS: frozenset[str]` and consistent readiness checks in `ProjectWorkbenchService.resolve_context` and `WorkbenchSessionCoordinator.create`.

- [ ] **Step 1: Write failing readiness tests**

Add tests proving `srt_full_pose` has `can_open_workbench == False` without a current trajectory job, that `WorkbenchSessionCoordinator.create` rejects such a context, and that a current published trajectory changes capability to true and produces `launch_mode == "trajectory_ready"`.

```python
def test_full_pose_workbench_requires_validated_trajectory(...):
    context = replace(_context(), workflow="srt_full_pose", trajectory_job_id="", trajectory_run_id="", trajectory_output_revision="", trajectory_output_fingerprint="")
    with pytest.raises(WorkbenchPermissionDenied, match="validated trajectory"):
        coordinator.create("p1", "clip-1", return_to="/apps/project_workspace/?projectId=p1")
```

- [ ] **Step 2: Run the focused tests and confirm the old behavior fails**

Run: `python -m pytest tests/projects/test_workbench_sessions.py -k "full_pose and (trajectory or workbench)" -q`

Expected: at least one failure because full pose currently opens with `launch_mode == "workflow_start"`.

- [ ] **Step 3: Introduce one shared workflow set and use it in both readiness gates**

```python
TRAJECTORY_REQUIRED_WORKBENCH_WORKFLOWS = frozenset({
    "srt_full_pose",
    "srt_fixed_track_visual_pose",
})

if context.workflow in TRAJECTORY_REQUIRED_WORKBENCH_WORKFLOWS and not trajectory_ready:
    raise WorkbenchPermissionDenied("SRT workbench requires a validated trajectory")
```

In `resolve_context`, require `current_job is not None` whenever `clip.resolved_workflow` belongs to that same set.

- [ ] **Step 4: Run the focused backend tests**

Run: `python -m pytest tests/projects/test_workbench_sessions.py -k "full_pose and (trajectory or workbench)" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the readiness gate**

```powershell
git add cadscene/projects/workbench_sessions.py tests/projects/test_workbench_sessions.py
git commit -m "fix: require full-pose trajectory before workbench"
```

---

### Task 2: Automatically Prepare Full-Pose Trajectory Artifacts

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: `ProjectService.preflight_trajectory_jobs`, `enqueue_trajectory_jobs`, clip `resolved_workflow`, clip progress/status.
- Produces: HTTP `202` payload `{state: "preparing_trajectory", message, job_id, jobs_revision}` for both SRT trajectory workflows and accurate full-pose progress text.

- [ ] **Step 1: Write failing API and static UI tests**

Add an API test where a physically prepared, configured `srt_full_pose` clip has no trajectory output. POSTing its workbench-session endpoint must return `202`, `state == "preparing_trajectory"`, a message containing `SRT 全姿态轨迹`, and a queued job using adapter `srt_full_pose`. Add a static test requiring separate preparation labels for `srt_full_pose` and `srt_fixed_track_visual_pose`.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py -k "full_pose and prepar" -q`

Expected: failure because `_create_workbench_session` only queues fixed-track trajectories and the UI uses fixed-track copy for all trajectory preparation.

- [ ] **Step 3: Generalize trajectory preparation without changing pure/SfM behavior**

Use the shared trajectory-required workflow rule in `_create_workbench_session`. For `srt_full_pose`, return `"正在生成 SRT 全姿态轨迹"`; retain `"正在生成 SRT 固定轨迹并估计视觉姿态"` for fixed-track. Only fall back to `enqueue_workbench_clip_export` when the physical clip is actually missing.

In `waitForWorkbenchPreparation`, derive the current workflow from the polled clip and use:

```javascript
const fullPose = clip?.resolved_workflow === "srt_full_pose";
const trajectoryLabel = fullPose ? "SRT 全姿态轨迹" : "SRT 固定轨迹与视觉姿态";
```

Use `trajectoryLabel` for the title, fallback progress message, and failure message.

- [ ] **Step 4: Run the focused tests**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py -k "full_pose and prepar" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit automatic preparation**

```powershell
git add cadscene/projects/http_api.py apps/project_workspace/project_workspace.js tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py
git commit -m "feat: prepare full-pose track before workbench"
```

---

### Task 3: Make Integer FOV the End-to-End Contract

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/projects/test_full_pose_configuration.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: user input and historical `horizontal_fov_deg` values.
- Produces: `_normalize_horizontal_fov_deg(value: float) -> int` returning an integer in `[2, 178]`; integer JSON values passed to both SRT adapters.

- [ ] **Step 1: Write failing normalization tests**

Add backend tests calling both settings methods with `59.11` and asserting stored `horizontal_fov_deg == 59`. Add boundary tests for values that round below `2` or above `178`. Add static UI assertions for `min="2"`, `max="178"`, `step="1"`, and `Math.round` during dialog population and submission.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `python -m pytest tests/projects/test_full_pose_configuration.py tests/viewer/test_project_workspace_static.py -k "fov or full_pose" -q`

Expected: failures because `59.11` is stored/displayed as a decimal and the HTML step is `0.1` from a `1.01` minimum.

- [ ] **Step 3: Implement the integer normalizer and browser behavior**

```python
from math import floor

def _normalize_horizontal_fov_deg(value: float) -> int:
    fov = float(value)
    if not isfinite(fov):
        raise ValueError("horizontal_fov_deg must be finite")
    rounded = floor(fov + 0.5)
    if not 2 <= rounded <= 178:
        raise ValueError("horizontal_fov_deg must round inside [2, 178]")
    return int(rounded)
```

Use it in both SRT settings methods. Change the HTML input to integer constraints. Populate it with `Math.round(Number(settings.horizontal_fov_deg))` and submit `Math.round(Number(fovInput.value))`; validate `[2, 178]` and update the Chinese validation copy accordingly.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/projects/test_full_pose_configuration.py tests/viewer/test_project_workspace_static.py -k "fov or full_pose" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit integer FOV**

```powershell
git add cadscene/projects/service.py apps/project_workspace/index.html apps/project_workspace/project_workspace.js tests/projects/test_full_pose_configuration.py tests/viewer/test_project_workspace_static.py
git commit -m "fix: use integer FOV for SRT workflows"
```

---

### Task 4: Show the Correct Full-Pose Workbench and Live Pose

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: the already loaded authoritative `cameraTrack`, interpolated `camera`, `currentFrame()`, and `sfm_viewer_scene.json` route.
- Produces: `#fullPoseLivePoseStatus` and `updateFullPoseCurrentFrameInfo(frame)` showing frame, XYZ, yaw/pitch/roll, and integer FOV; full-pose UI always enters `keyframes` and hides SfM controls.

- [ ] **Step 1: Write failing workbench UI tests**

Require that full-pose mode hides `#workflowStartSfm`, that `detectWorkflowStageFromArtifacts` returns `keyframes` for full pose without falling through to video/CAD/SfM detection, that pending full-pose sessions show a trajectory-unavailable recovery message instead of `开始 SfM 重建`, and that the HTML/viewer contain a live pose status updated from `camera` with `Math.round(camera.fov)`.

- [ ] **Step 2: Run focused UI tests and confirm failure**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -k "full_pose" -q`

Expected: failures because the current defensive paths select `sfm`, the start-SfM button remains visible, and no live pose readout exists.

- [ ] **Step 3: Remove every full-pose SfM fallback**

In `applyPureRotationWorkflowLayout`, hide `#workflowStartSfm` for `pure || fixed || full`. In `detectWorkflowStageFromArtifacts`, return `keyframes` for full pose, while writing an explicit missing-trajectory status if the artifact probe fails. In project-session bootstrap and job polling, never call `setWorkflowStage("sfm")` for full pose; choose `keyframes` and show `SRT 全姿态轨迹尚未准备，请返回项目管理页重新生成` as the defensive recovery message. Exclude full pose from any SfM completion/initialization branch.

- [ ] **Step 4: Add and wire live pose telemetry**

Add this read-only element inside `#fullPoseAdjustmentPanel`:

```html
<output id="fullPoseLivePoseStatus" class="full-pose-live-pose" aria-live="polite">
  正在读取当前帧轨迹与姿态…
</output>
```

Update it from the interpolated camera on playback, seeking, track load, and whole-route offset:

```javascript
function updateFullPoseCurrentFrameInfo(frame) {
  const el = document.querySelector("#fullPoseLivePoseStatus");
  if (!el || !camera) return;
  el.textContent = `帧 ${frame}　位置 X ${camera.x.toFixed(3)} / Y ${camera.y.toFixed(3)} / Z ${camera.z.toFixed(3)} 米　姿态 yaw ${camera.yaw.toFixed(2)}° / pitch ${camera.pitch.toFixed(2)}° / roll ${camera.roll.toFixed(2)}°　FOV ${Math.round(camera.fov)}°`;
}
```

Call it from `updateTrackStatus` and after `applyTrackPayload`/route offset updates. Keep the existing scene load and `syncSfmAnchoredTrackFromCurrentTrack` so the whole route and current frustum remain visible.

- [ ] **Step 5: Run focused UI tests**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -k "full_pose" -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit the dedicated workbench UI**

```powershell
git add apps/web_camera_viewer/index.html apps/web_camera_viewer/style.css apps/web_camera_viewer/workflow.js apps/web_camera_viewer/viewer_legacy.js tests/viewer/test_workflow_ui_static.py
git commit -m "feat: show live full-pose route telemetry"
```

---

### Task 5: End-to-End Verification on the Current Project

**Files:**
- Verify: `work/srt-full-pose-ui-test/projects/p-4550302b3ede4a51/`
- Verify: generated run under `work/srt-full-pose-ui-test/runs/p-4550302b3ede4a51-clip-0001/clip-0001/`

**Interfaces:**
- Consumes: project snapshot and local service on port `8310`.
- Produces: a current successful full-pose trajectory job, a `trajectory_ready` session, and a browser-ready workbench URL.

- [ ] **Step 1: Run the complete focused regression set**

Run: `python -m pytest tests/projects/test_workbench_sessions.py tests/projects/test_full_pose_configuration.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py -q`

Expected: all tests pass.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`

Expected: all tests pass, with only the repository's documented skips.

- [ ] **Step 3: Restart the local service from the feature worktree**

Stop only the process listening on `127.0.0.1:8310`, verify the resolved command line points at this workspace service, then restart with the existing storage root `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test` and a hidden window.

- [ ] **Step 4: Normalize the current project FOV and trigger preparation through the public API**

Fetch the current snapshot/revisions, PATCH the full-pose settings to `59`, POST the workbench-session endpoint, poll the snapshot until the trajectory job succeeds and `can_open_workbench` becomes true, then POST again. Assert:

```text
resolved_workflow = srt_full_pose
horizontal_fov_deg = 59
trajectory status = success
can_open_workbench = true
launch_mode = trajectory_ready
resume_state.workflow_stage = keyframes
```

- [ ] **Step 5: Verify published artifacts and workbench contract**

Check that all three files exist and are non-empty:

```text
02_srt_full_pose/camera_trajectory_full_pose.json
03_alignment/camera_track_pred.json
05_viewer_scene/sfm_viewer_scene.json
```

Verify the camera track contains many frames with changing position and attitude, integer `fov == 59`, and the viewer scene contains a non-empty route. Open the resulting workbench URL and confirm its initial stage is `轨迹微调`, with no SfM action, the route visible, the current frustum visible, and live pose text changing when the video seeks or plays.

- [ ] **Step 6: Commit any verification-only test adjustments and leave the service running**

Stage only task-owned files, review `git diff --cached`, commit if needed, and leave `http://127.0.0.1:8310/apps/project_library/` available for the user's next test.
