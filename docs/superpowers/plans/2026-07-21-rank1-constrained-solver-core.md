# Rank-1 SfM→CAD Constrained Solver Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline Rank-1 solver that uses partial-SRT only for along-track metric constraints and qualified manual solve/validate anchors for SfM→CAD pose and hold-out verification.

**Architecture:** `rank1_constrained.py` owns rank analysis, robust 1D scale, manual-prior transform recovery, CAD trajectory construction, and bounded along-track correction. `rank1_validation.py` owns hold-out metrics. The CLI composes existing SfM, synchronized SRT CSV, schema-v2 keyframe, and atomic output helpers without changing standard Sim3 or product routing.

**Tech Stack:** Python 3.10+, NumPy, standard-library argparse/csv/json/tempfile, pytest, existing `SfmTrajectory`, SRT fusion quaternion helpers, and orientation-prior schema.

## Global Constraints

- Do not modify the standard rank>=2 Sim3 implementation or its quality gate.
- Do not add frontend, Job Runner, workflow routing, GPS projection, point-cloud rendering, or Stage 6B-2 integration.
- Do not read or commit real video, SRT, GPS, camera-track, `data/`, or `runs/` content.
- Use deterministic robust estimation with an explicit random seed and fail closed on missing/invalid priors.
- Formal trajectory output requires distinct qualified solve and validate anchors.
- Every production behavior is introduced by a failing synthetic test first.

---

### Task 1: Rank-1 axis and robust along-track scale

**Files:**
- Create: `cadscene/alignment/rank1_constrained.py`
- Create: `tests/alignment/test_rank1_constrained.py`
- Create: `docs/superpowers/plans/2026-07-21-rank1-constrained-solver-core.md`

**Interfaces:**
- `analyze_rank1_axis(points, times, config) -> Rank1AxisAnalysis`
- `project_along_track(points, direction, reference) -> np.ndarray`
- `estimate_along_track_scale(u_sfm, u_srt, config) -> AlongTrackScaleFit`
- `Rank1Config` contains rank/linearity/baseline, scale, RANSAC, prior-consistency, smoothing, and validation thresholds.

- [ ] Write synthetic tests for temporal sign disambiguation, known positive scale recovery, endpoint outlier rejection, and rank>=2 rejection.
- [ ] Run `python -m pytest tests/alignment/test_rank1_constrained.py -q` and verify import/behavior failures.
- [ ] Implement PCA/SVD axis analysis and deterministic 1D RANSAC using pair-derived slopes, median inlier refinement, and metric residual statistics.
- [ ] Re-run the focused test and verify all Task 1 tests pass.
- [ ] Commit `feat: estimate rank-1 SRT along-track scale`.

### Task 2: Manual-prior rotation and translation

**Files:**
- Modify: `cadscene/alignment/rank1_constrained.py`
- Modify: `tests/alignment/test_rank1_constrained.py`

**Interfaces:**
- `solve_rank1_transform(trajectory, scale_fit, sfm_direction, priors, qualifications, config) -> Rank1Transform`
- `Rank1Transform.apply_points(points) -> np.ndarray`
- Each candidate rotation uses `R_cad_from_camera_manual @ R_cam_from_sfm`; translation uses only solve-anchor camera centers.

- [ ] Add failing tests for known non-identity rotation/translation, unmatched solve frames, near-parallel prior rejection, and inconsistent multiple solve priors.
- [ ] Implement qualification enforcement, exact-frame pose lookup, proper-rotation validation, quaternion/SVD rotation averaging, angular-consistency rejection, and robust median translation.
- [ ] Run the focused tests and commit `feat: solve rank-1 SfM to CAD alignment`.

### Task 3: CAD trajectory output and along-track-only correction

**Files:**
- Modify: `cadscene/alignment/rank1_constrained.py`
- Modify: `tests/alignment/test_rank1_constrained.py`

**Interfaces:**
- `apply_along_track_correction(base_positions, d_cad, times, srt_u, valid, config) -> AlongTrackCorrection`
- `build_rank1_trajectory_json(raw, transform, corrected_positions, correction, meta) -> dict`
- Per-frame camera rotation follows `R_cam_from_cad = R_cam_from_sfm @ R_cad_from_sfm.T` using the existing quaternion transform helper.

- [ ] Add failing tests for normalized-projection equivalence, along-track-only movement, cross-track-noise isolation, jump suppression, gap behavior, and legacy loader compatibility.
- [ ] Implement segmented time-window median correction with no extrapolation and write the compatible `cad_meters` trajectory schema.
- [ ] Run focused tests and commit with Task 4 after validation is included.

### Task 4: Independent hold-out validation and failure protocol

**Files:**
- Create: `cadscene/alignment/rank1_validation.py`
- Create: `tests/alignment/test_rank1_validation.py`
- Modify: `cadscene/alignment/rank1_constrained.py`

**Interfaces:**
- `validate_holdout_anchors(trajectory, predicted_centers, transform, d_cad, priors) -> Rank1ValidationReport`
- Report contains position, along/cross/vertical, orientation, optional basis-angle errors, and projection-residual metadata for each validate frame.

- [ ] Add failing tests for position/orientation metrics, solve/validate isolation, and missing-holdout diagnostic-only behavior.
- [ ] Implement validation without passing validate anchors to scale, rotation, translation, or smoothing estimation.
- [ ] Run both alignment test files and commit `feat: validate rank-1 alignment with holdout anchors`.

### Task 5: Atomic CLI, reports, and integration

**Files:**
- Create: `cadscene/cli/align_rank1_srt_to_cad.py`
- Create: `tests/cli/test_align_rank1_srt_to_cad_cli.py`
- Create: `tests/integration/test_rank1_partial_srt_alignment.py`
- Create: `docs/superpowers/specs/2026-07-23-rank1-constrained-solver.md`
- Modify: `docs/workflow_routing.md`

**Interfaces:**
- CLI reads trajectory JSON, synchronized SRT sample CSV (`frame_index`, `frame_time_sec`, `east_m`, `north_m`, `up_m`, validity), and schema-v2 camera track.
- Success atomically writes six `03_rank1_alignment` artifacts; failure writes only `rank1_failure_report.md` and returns nonzero.

- [ ] Add failing CLI and integration tests for success artifacts, diagnostic-only missing validate, and invalid rank rejection.
- [ ] Implement parser/composition, report/CSV serializers, atomic writes, and stable error handling.
- [ ] Run CLI help, CLI tests, integration test, focused alignment tests, related regression, full regression, dependency check, and `git diff --check`.
- [ ] Add Chinese design/interface documentation and commit `feat: add rank-1 partial-SRT alignment CLI`.
- [ ] Push `feature/rank1-constrained-solver-core` without merging any branch.
