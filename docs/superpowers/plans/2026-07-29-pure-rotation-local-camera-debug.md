# Pure-Rotation Local-Camera Debug Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the Pure-Rotation debug workflow, camera-local rotation convention, and calibration controls without changing SfM/SRT behavior.

**Architecture:** Keep OpenGV Raw and CAD anchoring isolated. Add camera-local delta rotation helpers to the existing math module, expose authoritative matrices from the viewer, and apply local correction residuals in the Python correction backend. Keep world-up rotation as a separate left composition.

**Tech Stack:** JavaScript, Three.js TransformControls, Python, NumPy, SciPy Rotation/Slerp, pytest.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench_pure_rotation`.
- Do not modify the original cadscene worktree or the POC repository.
- Do not estimate or synthesize translation.
- Preserve existing SfM/SRT behavior.

---

### Task 1: Workflow and control surface

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/style.css`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/pure_rotation/test_viewer_contract.py`

- [ ] Add failing contracts for single numeric step circles, `调试` label, automatic success transition, moved correction actions, hidden Pure-Rotation suggestions, and mode-stable save/restore.
- [ ] Run the focused contracts and confirm failure.
- [ ] Implement the minimal markup, CSS, and workflow state changes.
- [ ] Run the focused contracts and confirm pass.

### Task 2: Camera-local rotation math

**Files:**
- Modify: `apps/web_camera_viewer/pure_rotation_math.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/pure_rotation/test_viewer_rotation_math.py`
- Test: `tests/pure_rotation/test_viewer_contract.py`

- [ ] Add failing tests for right-composed camera-local yaw/pitch/roll and unchanged world-up left composition.
- [ ] Run the focused tests and confirm failure.
- [ ] Implement `applyLocalCameraDelta`, local-space TransformControls, authoritative matrix capture, and manual-anchor seeding at current PTS.
- [ ] Run JavaScript syntax and focused math tests.

### Task 3: Local correction residual and SLERP

**Files:**
- Modify: `cadscene/pure_rotation/corrections.py`
- Test: `tests/pure_rotation/test_corrections.py`

- [ ] Add failing tests for `R_base.T @ R_manual` local residuals and `R_base @ DeltaR_local`.
- [ ] Run the focused tests and confirm failure.
- [ ] Change correction residual construction and final composition to camera-local right multiplication.
- [ ] Run correction and placement tests.

### Task 4: Verification and handoff

**Files:**
- Modify: `apps/web_camera_viewer/index.html` for cache version only.

- [ ] Run `python -m pytest tests/pure_rotation -q`.
- [ ] Run `python -m pytest tests/workflow tests/cli/test_serve_viewer_workflow_api.py -q`.
- [ ] Run `python -m pytest -q`.
- [ ] Run `python scripts/check_no_project_dependency.py` and `git diff --check`.
- [ ] Commit the implementation, restart port 8307 with the fixed external backend configuration, and report the new cache version.
