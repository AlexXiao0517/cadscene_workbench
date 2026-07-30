# Pure-Rotation Stage UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the experimental Pure-Rotation debug controls with a four-stage product workflow, add a world-vertical camera rotation slider, and automatically preview the SLERP-fitted correction trajectory before rendering.

**Architecture:** Keep the existing Python placement/correction APIs and authoritative SO(3) playback path. `workflow.js` owns stage state, draft placement, correction editing, automatic fitted-track reloads, and render handoff; `viewer_legacy.js` remains responsible for camera controls and direct matrix rendering; `pure_rotation_math.js` supplies side-effect-free rotation math used by the new slider.

**Tech Stack:** Python 3.10+, pytest, browser JavaScript, Three.js, existing cadscene viewer HTTP/API layer.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench_pure_rotation`.
- Do not modify `D:\zjic2026\cadscene_workbench` or `D:\zjic2026\pure_rotation_camera_poc`.
- Pure-Rotation runs only for no-SRT datasets explicitly marked `hovering_declared=true`.
- No-SRT default remains `sfm_only`; existing SRT routing remains unchanged.
- Do not estimate, infer, or fabricate translation.
- Keep the camera center fixed for every pose in a calibrated segment.
- Rotation matrices and unit quaternions are authoritative; Euler is display/input only.
- Do not interpolate Euler angles and do not interpolate across segments.
- Pure-Rotation has no quality stage and must not silently fall back to SfM.
- Preserve the current direct-matrix SO(3) rendering fix and PTS-based playback.

---

### Task 1: Lock the SO(3) Playback Baseline

**Files:**
- Modify: `tests/pure_rotation/test_viewer_contract.py`
- Modify: `tests/pure_rotation/test_viewer_rotation_math.py`
- Modify: `apps/web_camera_viewer/pure_rotation_math.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`

**Interfaces:**
- Consumes: `window.cadsceneApplyPureRotationPose(pose)` and `CadscenePureRotationMath.poseAtPts(poses, pts)`.
- Produces: a tested baseline where Three.js receives `rotation_cad_from_camera` directly and `camera_center_web` remains unchanged during playback.

- [ ] **Step 1: Add regression tests for direct-matrix rendering and fixed center**

```python
def test_pure_rotation_rendering_uses_authoritative_matrix_without_euler_round_trip():
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")
    assert "pureRotationAuthoritativeMatrix = rotation" in legacy
    assert "axesFromAuthoritativeRotation" in legacy


def test_pts_interpolation_uses_rotation_slerp_and_fixed_center():
    result = run_node(
        "m.poseAtPts([{pts_time_sec:0,segment_id:0,"
        "rotation_cad_from_camera:[[1,0,0],[0,1,0],[0,0,1]],camera_center_web:[1,2,3]},"
        "{pts_time_sec:1,segment_id:0,rotation_cad_from_camera:[[0,-1,0],[1,0,0],[0,0,1]],"
        "camera_center_web:[1,2,3]}],0.5)"
    )
    assert result["camera_center_web"] == [1, 2, 3]
```

- [ ] **Step 2: Run the focused baseline tests**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_viewer_rotation_math.py -q
```

Expected: tests pass; any failure identifies an incomplete existing SO(3) change that must be repaired before stage refactoring.

- [ ] **Step 3: Keep the minimum direct-matrix implementation**

```javascript
window.cadsceneApplyPureRotationPose = function (pose) {
  const rotation = pose.rotation_cad_from_camera || pose.rotation_local_from_camera;
  pureRotationPlaybackActive = true;
  pureRotationAuthoritativeMatrix = rotation;
  camera.x = Number(pose.camera_center_web[0]);
  camera.y = Number(pose.camera_center_web[1]);
  camera.z = Number(pose.camera_center_web[2]);
  updateViews();
};
```

- [ ] **Step 4: Re-run the focused baseline tests**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_viewer_rotation_math.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit the baseline**

```powershell
git add apps/web_camera_viewer/pure_rotation_math.js apps/web_camera_viewer/viewer_legacy.js tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_viewer_rotation_math.py
git commit -m "fix: preserve authoritative pure-rotation playback"
```

### Task 2: Replace the Pure-Rotation Stage Navigation

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Consumes: `trajectoryWorkflow.workflow_mode`.
- Produces: `applyPureRotationWorkflowLayout(true)` with visible stages `upload`, `sfm`, `keyframes`, `render`; the `sfm` slot is labeled “旋转轨迹恢复”.

- [ ] **Step 1: Replace the old static-contract assertions with product-stage assertions**

```python
def test_pure_rotation_uses_four_product_stages_and_has_no_quality_transition():
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert 'id="workflowStartPureRotation"' in html
    assert 'id="workflowEnterPureCalibration"' in html
    assert 'id="workflowPureFinishKeyframes"' in html
    assert 'data-pure-stage="quality"' not in html
    pure_next = workflow.split("const pureNext =", 1)[1].split("};", 1)[0]
    assert 'sfm: "keyframes"' in pure_next
    assert 'keyframes: "render"' in pure_next
    assert "quality" not in pure_next
```

- [ ] **Step 2: Run the contract test and confirm it fails**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py::test_pure_rotation_uses_four_product_stages_and_has_no_quality_transition -q
```

Expected: FAIL because the product-specific controls and stage semantics are not present.

- [ ] **Step 3: Add explicit product actions to the stage markup**

```html
<div class="workflow-actions workflow-hidden-control" id="pureRotationRecoveryActions">
  <button id="workflowStartPureRotation" data-job-action type="button">运行旋转轨迹恢复</button>
  <button id="workflowRerunPureRotation" data-job-action type="button">重新运行</button>
  <button id="workflowEnterPureCalibration" type="button" disabled>进入关键帧标定</button>
</div>
```

Keep the legacy quality markup available only to non-Pure workflows. In `applyPureRotationWorkflowLayout`, hide the quality step/panel for Pure-Rotation, rename the `sfm` slot, and renumber render to 4.

- [ ] **Step 4: Make successful recovery enable an explicit calibration transition**

```javascript
function updatePureRotationRecoveryActions(status) {
  const ready = status?.state === "succeeded" && Number(status?.summary?.pose_count || 0) > 0;
  document.querySelector("#workflowEnterPureCalibration").disabled = !ready;
}

document.querySelector("#workflowEnterPureCalibration")?.addEventListener("click", () => {
  setActiveStage("keyframes");
});
```

- [ ] **Step 5: Run focused UI contracts**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_upload_choice.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit the stage navigation**

```powershell
git add apps/web_camera_viewer/index.html apps/web_camera_viewer/workflow.js apps/web_camera_viewer/style.css tests/pure_rotation/test_viewer_contract.py
git commit -m "feat: present pure rotation as four workflow stages"
```

### Task 3: Move Calibration Controls out of the 3D Toolbar

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Consumes: `window.cadsceneGetCurrentCameraPose()` and the existing placement/correction APIs.
- Produces: `#pureRotationCalibrationPanel` with global placement and current-frame correction sections; no Pure-Rotation debug actions remain in `.panel-title .button-row`.

- [ ] **Step 1: Add failing structure and behavior tests**

```python
def test_pure_rotation_calibration_controls_live_below_camera_parameters():
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    toolbar = html.split('<div class="panel-title">', 1)[1].split("</div>", 2)[0]
    assert "pureRotationTrack" not in toolbar
    assert "pureRotationFocusCamera" not in toolbar
    assert 'id="pureRotationCalibrationPanel"' in html
    assert 'id="pureRotationSavePlacement"' in html
    assert 'id="pureRotationRestorePlacement"' in html
    assert 'id="pureRotationAddCorrection"' in html
    assert 'id="pureRotationPreviousCorrection"' in html
    assert 'id="pureRotationNextCorrection"' in html
```

- [ ] **Step 2: Run the new structure test**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py::test_pure_rotation_calibration_controls_live_below_camera_parameters -q
```

Expected: FAIL because the controls still occupy the 3D toolbar and restore/navigation controls do not exist.

- [ ] **Step 3: Add the stage-owned calibration panel**

```html
<section id="pureRotationCalibrationPanel" class="pure-rotation-calibration" hidden>
  <fieldset id="pureRotationPlacementSection">
    <legend>全局固定相机放置</legend>
    <p>位置、姿态和 FOV 调整会立即用于预览；保存仅用于确认当前方案。</p>
    <button id="pureRotationSavePlacement" type="button">保存当前全局放置</button>
    <button id="pureRotationRestorePlacement" type="button">恢复上次保存</button>
  </fieldset>
  <fieldset id="pureRotationCorrectionSection">
    <legend>当前帧姿态微调</legend>
    <button id="pureRotationAddCorrection" type="button">添加/更新当前关键帧</button>
    <button id="pureRotationDeleteCorrection" type="button">删除当前关键帧</button>
    <button id="pureRotationPreviousCorrection" type="button">上一个关键帧</button>
    <button id="pureRotationNextCorrection" type="button">下一个关键帧</button>
    <button id="pureRotationUndoDraft" type="button">撤销当前未保存调整</button>
  </fieldset>
</section>
```

- [ ] **Step 4: Remove debug-only event paths and preserve useful handlers**

Delete the track-layer selector, mode-switch buttons, camera-focus button, and export action from the Pure-Rotation product surface. Bind placement save/restore and correction add/delete/navigation only to controls inside `#pureRotationCalibrationPanel`.

- [ ] **Step 5: Lock x/y/z/fov during current-frame correction**

```javascript
window.cadsceneSetPureRotationEditMode?.("correction");
for (const key of ["x", "y", "z", "fov"]) pureRotationRestrictedFields.add(key);
```

The saved global placement remains the source of fixed center and display FOV.

- [ ] **Step 6: Run focused contracts**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the calibration panel**

```powershell
git add apps/web_camera_viewer/index.html apps/web_camera_viewer/workflow.js apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/style.css tests/pure_rotation/test_viewer_contract.py
git commit -m "feat: organize pure-rotation calibration controls"
```

### Task 4: Add World-Vertical Rotation Slider

**Files:**
- Modify: `apps/web_camera_viewer/pure_rotation_math.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `tests/pure_rotation/test_viewer_rotation_math.py`
- Modify: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Produces: `rotateAboutWorldUp(rotation: number[][], angleDeg: number, upAxis?: number[]): number[][]`.
- Consumes: current authoritative `rotation_cad_from_camera`.
- Produces: `window.cadsceneApplyManualPureRotationMatrix(rotation)` for previewing a correction draft without changing camera center.

- [ ] **Step 1: Add failing mathematical tests**

```python
def test_world_up_rotation_left_multiplies_without_moving_camera():
    result = run_node(
        "m.rotateAboutWorldUp([[1,0,0],[0,0,-1],[0,1,0]],90)"
    )
    expected = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    assert_matrix_close(result, expected)


def test_world_up_rotation_is_relative_to_drag_start_not_incrementally_accumulated():
    result = run_node(
        "const b=[[1,0,0],[0,1,0],[0,0,1]];"
        "JSON.stringify([m.rotateAboutWorldUp(b,10),m.rotateAboutWorldUp(b,20)])"
    )
    assert rotation_angle(result[1]) == pytest.approx(20.0)
```

- [ ] **Step 2: Run the math tests and verify failure**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_rotation_math.py -q
```

Expected: FAIL because `rotateAboutWorldUp` is not exported.

- [ ] **Step 3: Implement Rodrigues world-axis rotation and left multiplication**

```javascript
function axisAngleMatrix(axis, angleDeg) {
  const [x, y, z] = normalizeVector(axis);
  const a = Number(angleDeg) * Math.PI / 180;
  const c = Math.cos(a), s = Math.sin(a), t = 1 - c;
  return [
    [t*x*x+c, t*x*y-s*z, t*x*z+s*y],
    [t*x*y+s*z, t*y*y+c, t*y*z-s*x],
    [t*x*z-s*y, t*y*z+s*x, t*z*z+c],
  ];
}

function rotateAboutWorldUp(rotation, angleDeg, upAxis=[0, 0, 1]) {
  return multiply(axisAngleMatrix(upAxis, angleDeg), rotation);
}
```

Export `rotateAboutWorldUp`.

- [ ] **Step 4: Add slider, number input, and reset control**

```html
<label>绕竖直轴旋转
  <input id="pureRotationWorldYaw" type="range" min="-180" max="180" step="0.1" value="0" />
  <input id="pureRotationWorldYawNumber" type="number" min="-180" max="180" step="0.1" value="0" />
</label>
<button id="pureRotationResetWorldYaw" type="button">水平旋转归零</button>
```

- [ ] **Step 5: Apply slider values from an immutable frame baseline**

```javascript
let pureRotationCorrectionDraftBase = null;

function previewWorldYaw(angleDeg) {
  const rotation = CadscenePureRotationMath.rotateAboutWorldUp(
    pureRotationCorrectionDraftBase.rotation_cad_from_camera,
    Number(angleDeg),
  );
  window.cadsceneApplyManualPureRotationMatrix?.(rotation);
}
```

On seek or keyframe navigation, refresh `pureRotationCorrectionDraftBase` and reset both inputs to zero. Input events always derive from that baseline, never from the previous slider event.

- [ ] **Step 6: Verify position remains fixed in the viewer adapter**

```javascript
window.cadsceneApplyManualPureRotationMatrix = function (rotation) {
  pureRotationPlaybackActive = true;
  pureRotationAuthoritativeMatrix = rotation;
  const fixedCenter = [camera.x, camera.y, camera.z];
  updateViews();
  [camera.x, camera.y, camera.z] = fixedCenter;
};
```

- [ ] **Step 7: Run math and UI tests**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_rotation_math.py tests/pure_rotation/test_viewer_contract.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the slider**

```powershell
git add apps/web_camera_viewer/pure_rotation_math.js apps/web_camera_viewer/workflow.js apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/index.html tests/pure_rotation/test_viewer_rotation_math.py tests/pure_rotation/test_viewer_contract.py
git commit -m "feat: rotate calibration camera about world vertical"
```

### Task 5: Auto-Fit Corrections and Hand Off Directly to Render

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `tests/pure_rotation/test_viewer_contract.py`
- Modify: `tests/pure_rotation/test_corrections.py`
- Modify: `tests/cli/test_serve_viewer_workflow_api.py`

**Interfaces:**
- Consumes: `POST /api/pure-rotation/corrections` and `GET /api/pure-rotation/trajectory?track=corrected`.
- Produces: `refreshPureRotationFittedPreview()` and a direct `keyframes → render` transition.

- [ ] **Step 1: Add failing auto-fit and render-handoff contracts**

```python
def test_correction_changes_reload_fitted_preview_and_render_has_back_action():
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert "refreshPureRotationFittedPreview" in workflow
    assert 'id="workflowPreviewPureFitted"' in html
    assert 'id="workflowReturnPureCalibration"' in html
    assert 'keyframes: "render"' in workflow
    assert "拟合后将进入质量检测" not in workflow
```

- [ ] **Step 2: Run the new contract and correction tests**

Run:

```powershell
python -m pytest tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_corrections.py -q
```

Expected: the new contract fails; existing Python SLERP tests pass.

- [ ] **Step 3: Reload the corrected track after every correction mutation**

```javascript
async function refreshPureRotationFittedPreview() {
  pureRotationTrajectory = await loadPureRotationTrack(
    pureRotationCorrections.length ? "corrected" : "base",
  );
  applyPureRotationPose();
}
```

Call this after successful add/update and delete API requests. Do not expose a Raw/Base/Corrected selector.

- [ ] **Step 4: Add final calibration and render controls**

```html
<button id="workflowPureFinishKeyframes" type="button">完成标定并生成拟合轨迹</button>
<button id="workflowPreviewPureFitted" type="button">预览拟合结果</button>
<button id="workflowReturnPureCalibration" type="button">返回关键帧标定</button>
```

- [ ] **Step 5: Validate before entering render**

```javascript
async function finishPureRotationCalibration() {
  if (!pureRotationHasPlacement) throw new Error("请先保存全局固定相机放置");
  await refreshPureRotationFittedPreview();
  setActiveStage("render");
}
```

The corrected trajectory remains fixed-center and uses the existing Python residual/SLERP implementation.

- [ ] **Step 6: Remove Pure-Rotation quality-stage language and transition**

Keep the existing quality controls and API behavior for SfM/SRT workflows only. Pure-Rotation’s stage order and completion path must not reference quality.

- [ ] **Step 7: Run focused, workflow, and API tests**

Run:

```powershell
python -m pytest tests/pure_rotation -q
python -m pytest tests/workflow tests/cli/test_serve_viewer_workflow_api.py -q
```

Expected: PASS, except any pre-existing unrelated test explicitly documented with its unchanged failure signature.

- [ ] **Step 8: Run static and full regression checks**

Run:

```powershell
python -m pytest tests/alignment tests/core tests/workflow -q
python -m pytest
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: all relevant regressions pass; no project/cadvideo dependency is introduced; no whitespace errors.

- [ ] **Step 9: Browser smoke**

Open the Stage 7A smoke dataset and verify:

1. Pure-Rotation displays four stages and no quality stage.
2. Recovery loads the existing 906-pose trajectory.
3. Draft placement plays without requiring save.
4. The world-vertical slider changes heading while preserving fixed center and current tilt.
5. Three-axis Gizmo still performs fine adjustment.
6. Add/update/delete correction automatically refreshes the smooth fitted preview.
7. Seek and playback use PTS and do not introduce multi-turn artifacts.
8. Completing calibration enters render directly.

- [ ] **Step 10: Commit the fitted render handoff**

```powershell
git add apps/web_camera_viewer/index.html apps/web_camera_viewer/workflow.js apps/web_camera_viewer/style.css tests/pure_rotation/test_viewer_contract.py tests/pure_rotation/test_corrections.py tests/cli/test_serve_viewer_workflow_api.py
git commit -m "feat: fit pure-rotation keyframes before render"
```
