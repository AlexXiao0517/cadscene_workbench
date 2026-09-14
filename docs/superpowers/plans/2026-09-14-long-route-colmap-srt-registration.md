# Long-route COLMAP/SRT Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent valid long-route incomplete-SRT reconstructions from failing solely because a fixed 5 m registration threshold is too small.

**Architecture:** Derive the RANSAC threshold from the authoritative SRT route span while retaining the existing 5 m floor and 50% inlier safety rule. Keep exact SRT centers in all outputs and expose the effective threshold in diagnostics.

**Tech Stack:** Python 3, NumPy, SciPy, pytest

## Global Constraints

- SRT projected CAD-local centers remain authoritative.
- COLMAP supplies orientation only.
- Adaptive tolerance is exactly `max(5.0, 0.01 * route_span_m)` for the default API behavior.
- Explicit caller-provided `max_alignment_residual_m` remains an absolute override.
- Existing geometry and minimum-inlier rejection rules remain active.

---

### Task 1: Route-relative registration tolerance

**Files:**
- Modify: `cadscene/srt/colmap_pose_transfer.py`
- Test: `tests/srt/test_colmap_pose_transfer.py`

**Interfaces:**
- Consumes: `transfer_colmap_pose_to_srt(reconstruction, positions, *, max_alignment_residual_m=None, ransac_iterations=256)`
- Produces: `ColmapPoseTransfer.alignment_threshold_m` and unchanged exact-SRT `centers`

- [ ] **Step 1: Write the failing long-route test**

Add a synthetic non-degenerate route spanning about 3 km, apply a smooth 8–12 m deformation to the authoritative centers, call the default transfer API, and assert that it succeeds, preserves every SRT center exactly, and reports an effective threshold near 30 m.

- [ ] **Step 2: Run the new test and verify the fixed 5 m behavior fails**

Run: `D:\anaconda3\python.exe -m pytest tests/srt/test_colmap_pose_transfer.py::test_transfer_scales_default_threshold_for_long_routes -q`

Expected: FAIL with `ColmapPoseTransferError` because the current implementation always uses 5 m.

- [ ] **Step 3: Implement the minimal adaptive default**

Change the optional threshold default to `None`, compute route span from `np.ptp(target, axis=0)`, use `max(5.0, 0.01 * route_span_m)` only when the caller omitted the threshold, and store the selected threshold in the returned transfer object. Preserve validation for explicit non-positive or non-finite values.

- [ ] **Step 4: Add and run the rejection test**

Add a long-route case whose non-Sim3 deformation exceeds the 1% threshold for more than half the frames and assert `ColmapPoseTransferError`. Run the complete transfer test module and expect all tests to pass.

- [ ] **Step 5: Expose the threshold in orientation diagnostics**

Add `alignment_threshold_m` beside the existing residual statistics and assert it in the orientation-solution test.

- [ ] **Step 6: Verify the focused and complete suites**

Run the focused SRT tests, then `D:\anaconda3\python.exe -m pytest -q`. Expected: all tests pass with only the repository's documented skips.

- [ ] **Step 7: Validate against the existing real reconstruction and commit**

Run only `cadscene.cli.build_srt_fixed_track_visual_pose` against attempt 3's existing `camera_trajectory.json` and `sparse_points.ply`. Expected: exit code 0 and generated `02_srt_visual_pose`, `03_alignment`, and `05_viewer_scene` directories. Stage only the two source/test files and these two design documents, then commit with `fix: scale COLMAP SRT tolerance for long routes`.
