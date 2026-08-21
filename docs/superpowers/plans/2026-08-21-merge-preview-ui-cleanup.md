# Merge Result Preview And Workbench Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep merged-video playback inside the project page with an explicit download action, and remove obsolete manual test-file controls from the workbench.

**Architecture:** The existing merge video endpoint remains the streaming source for the HTML video element. A `download=1` query switches only that response to attachment disposition. The project page owns a native dialog containing the preview and download link; obsolete controls are removed from markup and their mandatory event bindings are removed without changing project APIs or persisted data.

**Tech Stack:** Python `http.server`, vanilla HTML/CSS/JavaScript, pytest static and HTTP integration tests.

## Global Constraints

- Do not change project, clip, trajectory, render, or merge persistence contracts.
- Do not implement sidebar project files, overview, or settings in this change.
- Keep rendered-video preview endpoints inline; attachment disposition applies only to the merged-video download request.
- Preserve old internal import/export helpers for compatibility while removing their production UI entry points.

---

### Task 1: Merge result preview and explicit download

**Files:**
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `tests/cli/test_serve_viewer_project_api.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/style.css`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `cadscene/cli/serve_viewer.py`

**Interfaces:**
- Consumes: `snapshot.merge.download_url: string | null`
- Produces: `openMergeResult(downloadUrl)` and `GET /api/projects/<id>/merge-output/video?download=1`

- [ ] Add failing static tests requiring an in-page video dialog and forbidding `window.location.assign` for merge download.
- [ ] Add a failing HTTP integration test requiring `Content-Disposition: attachment` only when `download=1`.
- [ ] Run the focused tests and confirm they fail for the missing behavior.
- [ ] Add the dialog, preview video, close action, and explicit download anchor.
- [ ] Parse the request query and pass an optional attachment filename to `_send_file_head` for merged-video downloads only.
- [ ] Run the focused tests and confirm they pass.
- [ ] Commit the task.

### Task 2: Remove obsolete workbench test controls

**Files:**
- Modify: `tests/viewer/test_web_viewer_static.py`
- Modify: `tests/pure_rotation/test_viewer_contract.py`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/workflow.js`

**Interfaces:**
- Removes production DOM entry points: `importSfmScene`, `exportCamera`, `importCamera`, and `importSuggestions`.
- Retains existing project-driven loading, camera editing, quality review, and persistence paths.

- [ ] Add failing static tests asserting that the four obsolete controls are absent and formal controls remain present.
- [ ] Run the focused tests and confirm they fail on the current markup.
- [ ] Remove the four controls and their direct event bindings; remove layout references that assume `exportCamera` exists.
- [ ] Run the focused tests and confirm they pass.
- [ ] Run all viewer/CLI tests, full pytest, dependency check, and `git diff --check`.
- [ ] Commit the task, merge to `main`, rerun focused verification on main, and push `origin/main`.
