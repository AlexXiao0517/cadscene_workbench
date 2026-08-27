# SfM FOV Refresh Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a refreshed SfM workbench restores the FOV computed from SfM intrinsics instead of falling back to 70°, without overwriting a saved camera track.

**Architecture:** Replace cross-refresh `sessionStorage` deduplication with page-instance state in `workflow.js`. Expose a read-only saved-track predicate from `viewer_legacy.js` so the workflow can preserve authoritative manual/aligned tracks.

**Tech Stack:** Browser JavaScript, pytest static contract tests

## Global Constraints

- Do not modify SfM, alignment mathematics, project manifests, or saved camera tracks.
- Saved manual/aligned tracks take precedence over SfM initialization.
- Pure Rotation behavior must remain unchanged.

---

### Task 1: Add refresh and track-priority regression contracts

**Files:**
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: `apps/web_camera_viewer/workflow.js`, `apps/web_camera_viewer/viewer_legacy.js`
- Produces: static contracts for page-local deduplication and saved-track priority

- [x] **Step 1: Write the failing tests**

Add assertions that Workflow has page-local initialization state, does not read/write the old `cadsceneSfmCameraInit` session key, and checks `cadsceneHasLoadedCameraTrack`. Assert that Viewer exposes `window.cadsceneHasLoadedCameraTrack` and marks a successfully loaded track authoritative.

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -q`

Expected: FAIL because the old cross-refresh session key remains and no saved-track predicate exists.

### Task 2: Implement page-local initialization and saved-track priority

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`

**Interfaces:**
- Consumes: `window.cadsceneHasLoadedCameraTrack(): boolean`
- Produces: page-local `sfmCameraInitializationPromise` and `sfmCameraInitializationComplete` state

- [x] **Step 1: Add the minimal Viewer predicate**

Track whether `applyTrackPayload` has loaded a saved/imported payload and expose it through `window.cadsceneHasLoadedCameraTrack`.

- [x] **Step 2: Replace cross-refresh deduplication**

Use a shared in-page promise to combine concurrent calls. Skip SfM application when Viewer reports a loaded track; otherwise fetch and apply the current SfM FOV.

- [x] **Step 3: Run focused tests**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py tests/pure_rotation/test_viewer_contract.py tests/sfm/test_camera_init.py -q`

Expected: PASS.

- [x] **Step 4: Run full regression and repository checks**

Run: `python -m pytest -p no:cacheprovider -q`, `python scripts/check_no_project_dependency.py`, and `git diff --check`.

Expected: all tests and checks pass.
