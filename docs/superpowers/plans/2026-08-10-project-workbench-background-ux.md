# Project Workbench Background UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep project trajectory/render jobs running after leaving the workbench while presenting truthful progress, inline project renaming, shared themes, and clearer workbench controls.

**Architecture:** Project jobs remain owned by `ProjectService` and the resource queue. The project snapshot owns user-visible task state; the workbench only saves editing state, enqueues project work, and navigates. Project names remain in `project_manifest.json` and are updated with optimistic revision checks.

**Tech Stack:** Python 3, atomic JSON repositories, static HTML/CSS/JavaScript, pytest.

## Global Constraints

- Never report 100% before a job is terminal `success`.
- Leaving the workbench must not cancel or terminate trajectory/render processes.
- Browser pages modify project state only through project APIs with `expected_revision`.
- Theme preference uses the same `mediaflow-theme` local-storage key as the upload page.
- No changes to SfM, OpenGV, SRT, or Pure Rotation mathematical implementations.

---

### Task 1: Truthful project progress

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/cli/test_serve_viewer_project_api.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Produces: snapshot `progress.fraction` where active jobs never expose `1.0`; terminal success exposes `1.0`.

- [ ] Add failing tests for an active dependency whose adapter reports `fraction=1.0` and assert the snapshot exposes at most `0.99`.
- [ ] Run the focused API/static tests and confirm the new assertions fail.
- [ ] Add a presentation helper equivalent to:

```python
def _visible_job_progress(job):
    progress = dict(job.get("progress") or {})
    if job.get("status") in {"queued", "preparing", "running", "validating"}:
        if isinstance(progress.get("fraction"), (int, float)):
            progress["fraction"] = min(float(progress["fraction"]), 0.99)
    elif job.get("status") == "success":
        progress["fraction"] = 1.0
    return progress or None
```

- [ ] Make the project UI render 100% only for `status === "success"`; retain indeterminate display when no exact fraction exists.
- [ ] Run focused tests and commit.

### Task 2: Inline project rename

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/style.css`
- Test: `tests/cli/test_serve_viewer_project_api.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Produces: `PATCH /api/projects/{project_id}` with `{expected_revision, display_name}` and response `{project_revision, display_name}`.

- [ ] Add failing API tests for successful rename, blank/overlong rejection, and stale revision conflict.
- [ ] Add failing static tests for the pencil control, inline input, blur-save, Enter-save, and Escape-cancel.
- [ ] Run focused tests and confirm failure.
- [ ] Implement `ProjectService.update_project_display_name(project_id, expected_revision, display_name)` using the project repository.
- [ ] Add the PATCH route and return the new project revision.
- [ ] Implement inline editing without replacing the stable `project_id`; suspend snapshot repaint while the input is active.
- [ ] Run focused tests and commit.

### Task 3: Save-and-return workbench navigation and shared theme

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/style.css`
- Test: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Produces: `returnToProjectWorkspace()`; consumes the existing session `return_to`, `/close`, and save APIs.

- [ ] Add failing static tests for the top-right return button, confirmation dialog, no cancel API call, shared theme key, revised labels, removed trajectory-mode badge, and vertically centered buttons.
- [ ] Run tests and confirm failure.
- [ ] Add `persistWorkbenchDraftForReturn()` that saves current SfM camera track or confirmed Pure Rotation placement/corrections when available.
- [ ] Add a confirmation dialog stating that inference/rendering continues in the background.
- [ ] On confirmation, persist the draft, close only an editable session, set `projectWorkbenchInternalNavigation = true`, and navigate to `return_to` without cancelling any job.
- [ ] Reuse the upload page theme initialization and icon-only toggle.
- [ ] Rename the workbench and camera settings labels and remove the trajectory badge from layout.
- [ ] Run focused tests and commit.

### Task 4: Project-owned render launched from workbench

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Consumes: `POST /api/projects/{project_id}/render-jobs` and workbench `jobs_revision`.
- Produces: project render jobs visible through snapshot after navigation.

- [ ] Add failing tests that a project-bound render uses `/render-jobs`, does not call legacy `/api/workflow/run-stage`, and polls `clip.render` while the page remains open.
- [ ] Run tests and confirm failure.
- [ ] Change project-bound quality/debug completion to save without automatic navigation and enter the render stage.
- [ ] Implement render preflight/enqueue for the current clip and retain the returned `jobs_revision`.
- [ ] Poll project snapshot/runtime for visible progress while allowing the return button to navigate at any time.
- [ ] Run focused tests and commit.

### Task 5: Verification and service handoff

**Files:**
- Verify all files modified above.

- [ ] Run `node --check` for both modified JavaScript files.
- [ ] Run focused project, viewer, workflow, and Pure Rotation tests.
- [ ] Run `python -m pytest -q`.
- [ ] Restart port 8312 from the isolated worktree and verify the served assets contain the new controls.
- [ ] Verify the worktree is clean and report commits, tests, and the manual test sequence.
