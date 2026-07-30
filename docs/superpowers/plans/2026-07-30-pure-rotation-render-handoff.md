# Pure-Rotation Render Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore direct camera transform controls in Pure-Rotation debugging and provide a one-click, automatically persisted handoff to the existing isolated render/export stage.

**Architecture:** Keep the OpenGV raw trajectory unchanged. The browser maintains a live placement draft for immediate playback, persists that draft only on explicit confirmation or debug completion, fits manual orientation residuals with the existing SO(3)/SLERP implementation, and invokes the existing `render_pure_rotation` backend using the shared CAD overlay renderer with distance fading enabled.

**Tech Stack:** Vanilla JavaScript/HTML, Python job runner and CLI, pytest contract tests.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench_pure_rotation`.
- Do not modify the original cadscene worktree or the Pure-Rotation POC repository.
- Do not filter or rewrite the OpenGV raw rotation trajectory.
- Camera translation remains unobservable and the camera center remains fixed for every output pose.
- Pure-Rotation remains explicit opt-in and never falls back silently to SfM.

---

### Task 1: Restore debugging transform controls

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/index.html`
- Test: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Consumes: `window.cadsceneSetPureRotationEditMode(mode)` and Three.js transform controls.
- Produces: visible `#translateMode` and `#rotateMode` controls during Pure-Rotation debugging.

- [ ] Add contract tests requiring the camera toolbar to remain visible in Pure-Rotation mode, global placement to permit translate/rotate, and correction editing to prohibit center translation.
- [ ] Run `python -m pytest tests/pure_rotation/test_viewer_contract.py -q` and verify the new assertions fail because the toolbar is hidden.
- [ ] Remove the Pure-Rotation toolbar hiding rule and preserve mode-specific translation restrictions in the existing edit-mode adapter.
- [ ] Run the focused viewer contract tests and verify they pass.

### Task 2: Clarify live placement and completion behavior

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Consumes: existing placement save, correction save, and fitted-preview functions.
- Produces: `finishPureRotationCalibration()` that persists the live draft before entering render.

- [ ] Add failing contract tests for the labels “确认当前相机位置与方向”, “撤销未确认的调整”, and “完成调试并进入渲染”, plus call-order assertions that completion saves placement before fitting and changing stage.
- [ ] Run the focused test and verify failure on the old labels and missing auto-save call.
- [ ] Rename the controls, keep draft playback independent of saving, and update completion to save the current pose/FOV, regenerate the correction track, then enter render.
- [ ] Add explicit error handling that leaves the UI in debug mode when placement or fitted-track persistence fails.
- [ ] Run the focused viewer contract tests and verify they pass.

### Task 3: Enable shared distance fading for Pure-Rotation render

**Files:**
- Modify: `cadscene/workflow/job_runner.py`
- Test: `tests/pure_rotation/test_job_runner.py`

**Interfaces:**
- Consumes: `cadscene.cli.render_pure_rotation` and its `--faded-overlay`, `--fade-start-m`, and `--max-distance-m` options.
- Produces: render command using the same shared CAD overlay fading implementation as other workflows.

- [ ] Add a failing job-runner test asserting the Pure-Rotation render command selects corrected track over base track and includes `--faded-overlay`, `--fade-start-m 250`, and `--max-distance-m 900`.
- [ ] Run `python -m pytest tests/pure_rotation/test_job_runner.py -q` and verify failure because fading flags are absent.
- [ ] Add only the existing CLI flags to the Pure-Rotation render command; do not add orientation filtering.
- [ ] Run the focused job-runner tests and verify they pass.

### Task 4: Regression verification and commit

**Files:**
- Verify: all modified files above.

- [ ] Run `python -m pytest tests/pure_rotation -q`.
- [ ] Run `python -m pytest tests/workflow -q`.
- [ ] Run `python -m pytest tests/alignment tests/core tests/workflow -q`.
- [ ] Run `python -m pytest`.
- [ ] Run `git diff --check`.
- [ ] Verify the original cadscene worktree fingerprint and the POC worktree status are unchanged.
- [ ] Commit the isolated worktree changes with a focused message.
