# Scene Bridge Task UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make scene-bridge queue state, cancellation, prerequisites, modal behavior, and scheduling unambiguous without changing trajectory or bridge algorithms.

**Architecture:** Preserve the existing snapshot schema and queue resource classes. Add task-aware presentation at the project-workspace boundary, refine capability reasons using the existing trajectory job inventory, make the shared progress dialog dismissible without stopping polling, and raise only the scene-bridge queue priority.

**Tech Stack:** Python 3, pytest, vanilla JavaScript, HTML/CSS, existing project queue and repository architecture.

## Global Constraints

- Do not modify SfM, Pure Rotation, route fitting, or scene-bridge math.
- Do not run scene bridge concurrently with heavy trajectory work.
- Closing the modal must not cancel the backend job.
- Preserve existing snapshot and API compatibility.
- Implement every behavior test-first.

---

### Task 1: Accurate bridge prerequisite reasons and priority

**Files:**
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/projects/service.py`

**Interfaces:**
- Consumes: existing `QueueJob` records and `_current_trajectory_for_render` validation.
- Produces: direction-specific `locate_*_reason` messages and `scene_bridge` jobs with interactive priority.

- [ ] **Step 1: Add failing tests** for queued/running target trajectory messages and assert a newly queued scene bridge has priority above a normal trajectory job.
- [ ] **Step 2: Run the focused tests** with `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k "scene_bridge" -q` and confirm the new assertions fail for the old generic message/priority.
- [ ] **Step 3: Implement the minimum backend change** by selecting the latest active target trajectory status before the generic missing-output message and changing only scene-bridge priority.
- [ ] **Step 4: Re-run focused tests** and confirm all selected tests pass.

### Task 2: Task-aware row state and direction-specific errors

**Files:**
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_workspace/project_workspace.js`

**Interfaces:**
- Consumes: existing top-level `job_type/status/stage/progress` and `bridge_up_reason/bridge_down_reason`.
- Produces: explicit task status/cancel labels and separate up/down explanation text.

- [ ] **Step 1: Add failing static assertions** for a task-label helper, task-specific cancel text, and separate upward/downward reasons.
- [ ] **Step 2: Run** `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -q` and confirm the new assertions fail.
- [ ] **Step 3: Implement task-aware labels** without changing the job ID selected by the API, and render both directional reasons with prefixes instead of `up || down`.
- [ ] **Step 4: Re-run the static test file** and confirm it passes.

### Task 3: Dismissible progress dialog without accidental navigation

**Files:**
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/style.css`

**Interfaces:**
- Consumes: existing `workbenchPreparationDialog` polling loop.
- Produces: `closeWorkbenchPreparationDialog()` and dialog visibility state used to gate automatic navigation.

- [ ] **Step 1: Add failing assertions** for the close button, Escape cancellation handler, background continuation marker, and conditional workbench navigation.
- [ ] **Step 2: Run the static test file** and verify it fails for missing dialog controls.
- [ ] **Step 3: Add the close control and state handling** so close/Escape hide the modal but do not abort polling; success navigates only if the modal stayed open.
- [ ] **Step 4: Re-run the static test file** and confirm it passes.

### Task 4: Regression verification

**Files:**
- No production files.

**Interfaces:**
- Consumes: all changes from Tasks 1-3.
- Produces: verified repair ready for manual testing.

- [ ] **Step 1: Run focused project tests** with `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py -q`.
- [ ] **Step 2: Run broader project/API tests** with `python -m pytest -p no:cacheprovider tests/projects tests/cli/test_serve_viewer_project_api.py -q`.
- [ ] **Step 3: Run repository checks** with `python scripts/check_no_project_dependency.py` and `git diff --check`.
- [ ] **Step 4: Review `git status` and the final diff** to ensure no runtime workspace, jobs, or unrelated files are included.
