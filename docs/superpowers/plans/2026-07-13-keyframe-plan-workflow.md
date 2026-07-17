# Keyframe Plan Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the viewer run the initial-fit, keyframe-plan, final-fit, quality-check loop without treating planned frames as alignment anchors.

**Architecture:** Persist a small keyframe-plan artifact under the active run. The local viewer API creates and synchronizes it from the SfM trajectory and manual track; the legacy viewer remains responsible for video, CAD and camera editing. The workflow script only renders plan state, navigates to pending frames and starts the existing alignment/quality jobs.

**Tech Stack:** Python standard library, existing `cadscene.workflow`, legacy browser JavaScript, pytest.

## Global Constraints

- Keep `project/` read-only and do not import `project` or `cadvideo`.
- Do not introduce npm, React, Vite, or a browser build system.
- Preserve the legacy left video/right Three.js layout and bottom UAV controls.
- Reuse existing alignment and quality CLI stages; do not introduce a new alignment algorithm.

---

### Task 1: Persist and synchronize a keyframe plan

**Files:**
- Create: `cadscene/workflow/keyframe_plan.py`
- Modify: `cadscene/cli/serve_viewer.py`
- Test: `tests/workflow/test_keyframe_plan.py`

**Interfaces:**
- Produces `runs/<dataset>/<run_id>/01_keyframes/keyframe_plan.json`.
- `create_keyframe_plan(trajectory_path, camera_track, interval)` returns plan data and rejects intervals other than 120/180/240.
- `sync_keyframe_plan(plan_path, camera_track)` marks a planned frame complete only when an existing manual/confirmed keyframe has the same frame index.

- [ ] Write tests for a plan beginning at the first manual anchor, ending at the last SfM frame, and preserving the final frame.
- [ ] Implement the JSON plan writer and synchronization used after camera-track save.
- [ ] Add `POST /api/workflow/generate-keyframe-plan` and `GET /api/workflow/keyframe-plan`.

### Task 2: Put the keyframe panel in workflow order

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- The keyframe panel displays `路线拟合` before interval selection and `生成关键帧计划`.
- `workflowGenerateKeyframes` calls the local plan API instead of a mock message.
- `workflowFinishKeyframes` refuses to advance while planned frames remain pending; otherwise it asks the user to rerun route fitting before quality.

- [ ] Write static assertions for button order and no mock plan message.
- [ ] Load plan status and make “继续关键帧标定” jump to the earliest pending frame.
- [ ] Sync plan status after legacy add/update/delete buttons save the camera track.

### Task 3: Gate quality on final route fitting

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `cadscene/workflow/job_runner.py`
- Test: `tests/workflow/test_keyframe_plan.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- The alignment job records whether it was run after an existing plan and stores its source plan revision in the alignment status metadata.
- Quality refuses to start until every planned frame is complete and a later alignment job has succeeded.

- [ ] Write backend validation tests for incomplete plan and stale post-plan alignment.
- [ ] Add minimal workflow metadata checks before creating a quality command.
- [ ] Verify quality remains a check-only stage and does not run alignment itself.

### Task 4: Verify the loop

**Files:**
- Test: `tests/workflow/test_keyframe_plan.py`
- Test: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/cli/test_serve_viewer_workflow_api.py`

- [ ] Run focused workflow/API/static tests.
- [ ] Run the full project regression and `python scripts/check_no_project_dependency.py`.
