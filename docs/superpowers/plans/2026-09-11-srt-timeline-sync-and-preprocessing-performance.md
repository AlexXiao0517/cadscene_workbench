# SRT Timeline Sync And Preprocessing Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore an interactive SRT workbench timeline with bidirectional video/scene synchronization and remove duplicate/random video decoding from incomplete-SRT pose solving.

**Architecture:** A framework-free `frame_sync.js` module owns the logical source-frame selection and drag seek throttling, while `viewer_legacy.js` remains responsible for pose/CAD rendering and media I/O. The adaptive SRT planner becomes a one-pass sequential decoder that scores candidate frames and writes solve-resolution images in the same pass; `run_sfm` validates and reuses those prepared images instead of decoding the video again.

**Tech Stack:** Browser JavaScript, Three.js, HTML Canvas, Python 3, OpenCV, NumPy, pytest, COLMAP CLI.

## Global Constraints

- Preserve the existing SRT/CAD georeference, terrain, FOV, pose-fitting, keyframe, and rendering mathematics.
- `srt_full_pose` and `srt_fixed_track_visual_pose` show the shared trajectory/keyframe timeline; `pure_rotation` remains unchanged.
- Every user frame-selection path resolves to an exact source frame through the authoritative frame map and does not assume 30 FPS.
- Timeline dragging updates the right-hand pose immediately, throttles media seeks, and commits one exact seek on release.
- Right-hand route clicks seek the same logical frame; orbit/pan drags and route misses do not seek.
- Adaptive frame scoring and solve-image preparation use one monotonic video read and no per-candidate random seek.
- COLMAP may reuse prepared images only when their manifest is complete and matches video, SRT, frame map, resolution, and planner version.
- Existing user changes in the dirty worktree must be preserved.

---

### Task 1: Testable Logical Frame Coordinator

**Files:**
- Create: `apps/web_camera_viewer/frame_sync.js`
- Create: `tests/viewer/test_frame_sync.py`
- Modify: `apps/web_camera_viewer/index.html`

**Interfaces:**
- Produces: `window.CadsceneFrameSync.createCoordinator(options)` / CommonJS `createCoordinator(options)`.
- Produces: coordinator methods `selectFrame(frame, origin, options)`, `flushVideoSeek()`, `currentFrame()`, and `dispose()`.
- Consumes callbacks: `clampFrame`, `onLogicalFrame`, `seekVideo`, `schedule`, `cancel`, and `now`.

- [ ] **Step 1: Write the failing Node-backed pytest cases**

  Add tests that require `frame_sync.js` and assert: frames are rounded/clamped; a timeline drag immediately calls `onLogicalFrame`; repeated drag events coalesce media seeks; `flushVideoSeek()` commits the last frame; selecting the current frame from `video_seek` does not recursively seek video.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run: `python -m pytest tests/viewer/test_frame_sync.py -q`

  Expected: FAIL because `frame_sync.js` does not exist.

- [ ] **Step 3: Implement the minimal coordinator and load it before the legacy viewer**

  Implement a UMD module with a single logical frame value and one scheduled/throttled media-seek callback. Add `<script src="./frame_sync.js"></script>` before `viewer_legacy.js` in `index.html`.

- [ ] **Step 4: Run the focused tests and syntax checks**

  Run: `python -m pytest tests/viewer/test_frame_sync.py -q`

  Run: `node --check apps/web_camera_viewer/frame_sync.js`

  Expected: PASS.

### Task 2: Universal SRT Timeline And Bidirectional Scene Sync

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `tests/viewer/test_workflow_ui_static.py`
- Modify: `tests/viewer/test_web_viewer_static.py`

**Interfaces:**
- Consumes: `CadsceneFrameSync.createCoordinator` from Task 1.
- Produces: one internal `selectSourceFrame(frame, origin, options)` entry point used by video, timeline, frame input, keyframe navigation, and route picking.
- Produces: `pickSrtTrackRoute(event)` for both SRT workflows.

- [ ] **Step 1: Change static expectations and add failing interaction-source assertions**

  Assert that SRT modes do not hide `qualityTimelineWrap`, the visible heading is “轨迹与关键帧时间轴”, pointer drag handlers call the shared selection entry point, paused `seeking` updates it, and both full-pose and fixed-track modes use `pickSrtTrackRoute`.

- [ ] **Step 2: Run focused UI tests and verify RED**

  Run: `python -m pytest tests/viewer/test_workflow_ui_static.py tests/viewer/test_web_viewer_static.py -q`

  Expected: FAIL on the old `pure || fixed || full` visibility rule and missing shared selectors.

- [ ] **Step 3: Show and relabel the timeline for SRT modes**

  Keep the existing DOM id for compatibility, change the user-facing title, hide it only for pure rotation, and render pose-valid/invalid coverage plus planned/manual keyframes and the playhead for SRT workflows.

- [ ] **Step 4: Route all left-side frame changes through the coordinator**

  Connect video `timeupdate`, `seeking`, and `seeked`; timeline click/pointer drag; frame input; plan navigation; and existing `goToFrame` to `selectSourceFrame`. Update pose, CAD overlay, parameters, frustum, timeline, and keyframe save state from the logical source frame even while video is paused.

- [ ] **Step 5: Generalize route picking and suppress orbit-drag clicks**

  Pick the currently visible SRT track in either SRT mode, interpolate the source frame across the hit line segment, and invoke `selectSourceFrame(frame, "scene_track_pick")`. Record pointer-down displacement so Three.js orbit/pan drags do not trigger a seek.

- [ ] **Step 6: Run focused UI tests and browser JavaScript syntax checks**

  Run: `python -m pytest tests/viewer/test_frame_sync.py tests/viewer/test_workflow_ui_static.py tests/viewer/test_web_viewer_static.py -q`

  Run: `node --check apps/web_camera_viewer/workflow.js`

  Run: `node --check apps/web_camera_viewer/viewer_legacy.js`

  Expected: PASS.

### Task 3: One-Pass Adaptive Scoring And Solve-Image Preparation

**Files:**
- Create: `cadscene/sfm/adaptive_frame_preparation.py`
- Create: `tests/sfm/test_adaptive_frame_preparation.py`
- Modify: `cadscene/cli/plan_srt_adaptive_frames.py`
- Create or Modify: `tests/cli/test_plan_srt_adaptive_frames.py`

**Interfaces:**
- Produces: `prepare_adaptive_candidates(video_path, candidate_frames, output_dir, width, height, progress=None) -> PreparedCandidates`.
- Produces: a completion manifest containing planner version, input fingerprints, selected source-frame/image mapping, and file hashes.
- CLI adds `--images-output`, `--reconstruct-width`, and `--reconstruct-height`.

- [ ] **Step 1: Write failing one-pass and manifest tests**

  Use a fake capture whose `set()` raises and whose `read()` returns sequential numbered images. Assert candidates are scored during one monotonic read, selected images are named by source frame, unselected temporary images are removed, and an incomplete/mismatched manifest cannot be reused.

- [ ] **Step 2: Run focused backend tests and verify RED**

  Run: `python -m pytest tests/sfm/test_adaptive_frame_preparation.py tests/cli/test_plan_srt_adaptive_frames.py -q`

  Expected: FAIL because the preparation module and CLI flags do not exist.

- [ ] **Step 3: Implement one-pass decoding and candidate persistence**

  Sequentially call `capture.read()` from frame zero through the last candidate, resize only candidate frames, compute Laplacian variance from the same image, write candidates under a task-private temporary directory, then atomically publish selected images and a completion manifest.

- [ ] **Step 4: Integrate preparation with the existing SRT selection policy**

  Replace `_sharpness()` random seeks with the prepared candidate scores, keep the existing SRT motion/rotation/quality selection unchanged, and publish `source_frames` plus image identities in `adaptive_frame_plan.json`.

- [ ] **Step 5: Run focused tests and verify GREEN**

  Run: `python -m pytest tests/sfm/test_adaptive_frame_preparation.py tests/cli/test_plan_srt_adaptive_frames.py -q`

  Expected: PASS and the fake capture reports zero random seeks.

### Task 4: COLMAP Reuse Of Prepared Frames And Workflow Wiring

**Files:**
- Modify: `cadscene/cli/run_sfm.py`
- Modify: `cadscene/sfm/reconstruction.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `tests/sfm/test_reconstruction.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Modify: `tests/projects/test_service.py`

**Interfaces:**
- `run_sfm` adds `--prepared-images-dir` and validates it against `--source-frames-file`.
- `_fixed_track_plan_command()` writes prepared images to `<run-root>/02_sfm/images`.
- `_sfm_command()` passes that directory and skips `extract_frames()` only after exact manifest validation.

- [ ] **Step 1: Add failing CLI/adapter tests**

  Assert the fixed-track command sequence prepares `02_sfm/images`, the following SfM command passes `--prepared-images-dir`, prepared-image identity mismatches fail closed, and ordinary SfM still calls the existing extractor.

- [ ] **Step 2: Run focused tests and verify RED**

  Run: `python -m pytest tests/sfm/test_reconstruction.py tests/projects/test_workflow_adapters.py tests/projects/test_service.py -q`

  Expected: FAIL because the prepared-image option is absent.

- [ ] **Step 3: Implement strict prepared-image validation and extraction bypass**

  Compare selected source frames, deterministic filenames, dimensions, file hashes, and completion marker before reuse. On success start COLMAP feature extraction directly; on mismatch raise a descriptive error instead of silently mixing frames.

- [ ] **Step 4: Wire fixed-track commands and stage progress**

  Pass resolution and output directory into the planner, pass the prepared directory into SfM, update progress copy to distinguish “顺序解码并准备求解帧” from COLMAP, and preserve all non-fixed-track command behavior.

- [ ] **Step 5: Run focused tests and verify GREEN**

  Run: `python -m pytest tests/sfm/test_reconstruction.py tests/projects/test_workflow_adapters.py tests/projects/test_service.py -q`

  Expected: PASS.

### Task 5: Regression Verification And Live Workbench Smoke Test

**Files:**
- Modify only if a failing regression requires an in-scope correction.

**Interfaces:**
- Consumes all deliverables from Tasks 1–4.

- [ ] **Step 1: Run the SRT/backend regression suites**

  Run: `python -m pytest tests/viewer tests/srt tests/sfm tests/projects -q`

  Expected: PASS.

- [ ] **Step 2: Run repository status and JavaScript syntax verification**

  Run: `node --check apps/web_camera_viewer/frame_sync.js`

  Run: `node --check apps/web_camera_viewer/viewer_legacy.js`

  Run: `node --check apps/web_camera_viewer/workflow.js`

  Run: `git -c safe.directory=D:/zjic2026/cadscene_workbench/.worktrees/srt-full-pose-cad-georeference diff --check`

  Expected: all commands exit 0.

- [ ] **Step 3: Restart the existing local service from this worktree**

  Start the project service with storage `D:\zjic2026\cadscene_workbench\work\srt-full-pose-ui-test` at `http://127.0.0.1:8310` and confirm `/apps/project_library/` returns HTTP 200.

- [ ] **Step 4: Smoke-test the current SRT project in the browser**

  Verify the timeline is visible, dragging it updates the right camera/frustum immediately, video and timeline settle on the same frame after release, and clicking two separated route positions seeks the left video to the corresponding frames without firing after an orbit drag.

- [ ] **Step 5: Record measured scope and remaining performance boundary**

  Report that the current implementation removes random planning seeks and the second decode. If browser-compatible HEVC-to-H.264 preview transcode remains required, identify it separately from pose-solving time rather than claiming it was eliminated.
