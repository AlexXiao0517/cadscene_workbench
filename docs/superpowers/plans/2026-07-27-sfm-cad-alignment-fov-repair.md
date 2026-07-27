# SfM–CAD Alignment and FOV Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make two-anchor Sim3 alignment obey the CAD baseline, preserve a consistent manually confirmed horizontal FOV, and reject misleading high-residual exports.

**Architecture:** Keep the existing alignment pipeline and data contracts. Add focused helpers inside `cadscene/alignment/aligner.py`: a two-anchor baseline-constrained rotation, manual-FOV selection, and result validation. The point cloud and global track continue to consume the same Sim3; no SfM rerun is part of this plan.

**Tech Stack:** Python 3, NumPy, pytest, existing `cadscene` CLI and viewer exporter.

## Global Constraints

- Preserve current uncommitted FPS/timeline changes in the worktree.
- Do not rerun SfM.
- Horizontal FOV 67° from consistent confirmed manual keyframes takes precedence over the pathological trajectory FOV.
- The minimal repair must not claim that the existing point cloud is fully coplanar with CAD.
- Use failing regression tests before each production change.

---

### Task 1: Manual FOV selection

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Test: `tests/alignment/test_aligner.py`

**Interfaces:**
- Consumes: confirmed `KeyframeCorrespondence` values and the source manual track.
- Produces: `_manual_fov_from_track(track: Mapping[str, object]) -> float | None` and a selected FOV passed through path generation.

- [ ] **Step 1: Write the failing tests**

Add tests proving that two confirmed manual keyframes with FOV 67 make every generated path row and algorithm prediction use 67 even when trajectory FOV is 28.414. Add a second test proving inconsistent manual values fall back to the configured/trajectory behavior.

```python
def test_run_alignment_prefers_consistent_manual_fov_over_trajectory(tmp_path: Path) -> None:
    trajectory_path, track_path = _write_alignment_inputs(
        tmp_path, manual_fovs=(67.0, 67.0), trajectory_fx=3791.9127
    )
    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )
    assert {row["fov"] for row in result.sfm_camera_path_rows} == {67.0}
    assert {
        item["camera"]["fov"]
        for item in result.camera_track_pred["keyframes"]
        if item["source"] == "algorithm_prediction"
    } == {67.0}
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
pytest tests/alignment/test_aligner.py -k "manual_fov" -v
```

Expected: FAIL because the existing `_fov_for_path()` always selects `trajectory.horizontal_fov_deg()` when `fov_from="trajectory"`.

- [ ] **Step 3: Implement minimal manual-FOV precedence**

Add a helper that reads only confirmed manual keyframes, accepts finite FOV values in `(1, 179)`, and returns the value only when their spread is at most `0.1°`. Pass this selected value through the alignment configuration used by `_fov_for_path()` without changing manual keyframe payloads.

```python
def _manual_fov_from_track(track: Mapping[str, object]) -> float | None:
    values = [
        float((item.get("camera") or {}).get("fov"))
        for item in confirmed_keyframes(dict(track))
        if (item.get("camera") or {}).get("fov") is not None
    ]
    valid = [value for value in values if math.isfinite(value) and 1.0 < value < 179.0]
    if not valid or max(valid) - min(valid) > 0.1:
        return None
    return float(np.mean(valid))
```

- [ ] **Step 4: Run focused and alignment tests**

Run:

```powershell
pytest tests/alignment/test_aligner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit only Task 1 files**

```powershell
git add cadscene/alignment/aligner.py tests/alignment/test_aligner.py
git commit -m "fix: preserve confirmed manual fov in aligned route"
```

---

### Task 2: Baseline-constrained two-anchor Sim3

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Test: `tests/alignment/test_aligner.py`

**Interfaces:**
- Consumes: exactly two non-degenerate `KeyframeCorrespondence` objects.
- Produces: `_estimate_two_anchor_sim3(correspondences: Sequence[KeyframeCorrespondence]) -> Sim3`.

- [ ] **Step 1: Write a failing regression test for the real conflict shape**

Construct two SfM centers whose baseline has a large Z component, two equal-height CAD centers, and manual poses that conflict slightly with the positional baseline. Assert that the estimated Sim3 maps both centers to the CAD centers within `1e-6 m`.

```python
def test_two_anchor_sim3_hard_constrains_position_baseline() -> None:
    correspondences = _two_anchor_conflict_correspondences()
    estimated = estimate_global_sim3(correspondences)
    mapped = estimated.apply(np.asarray([row.center_sfm for row in correspondences]))
    expected = np.asarray([row.center_cad for row in correspondences])
    np.testing.assert_allclose(mapped, expected, atol=1e-6)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
pytest tests/alignment/test_aligner.py::test_two_anchor_sim3_hard_constrains_position_baseline -v
```

Expected: FAIL with nonzero endpoint position residual, reproducing the current 54.15 m failure mode.

- [ ] **Step 3: Implement baseline mapping plus twist selection**

For exactly two non-degenerate anchors:

1. Normalize source and destination baselines.
2. Compute the shortest rotation mapping the source direction to the destination direction, including a deterministic 180° fallback.
3. Search the single remaining twist around the destination baseline by minimizing the summed Frobenius error to the per-anchor orientation-derived target rotations.
4. Compute scale from baseline lengths and translation from the first anchor.

Use an analytic one-dimensional twist solution from projected orientation axes; do not add an optimizer dependency. Keep the current oriented-keyframe solver for coincident-position rotation-only mode and for three or more anchors.

- [ ] **Step 4: Run focused and complete alignment tests**

Run:

```powershell
pytest tests/alignment/test_aligner.py -v
```

Expected: PASS, including the existing test that two-anchor orientation determines the otherwise unobservable twist.

- [ ] **Step 5: Commit only Task 2 files**

```powershell
git add cadscene/alignment/aligner.py tests/alignment/test_aligner.py
git commit -m "fix: constrain two-anchor sim3 to cad baseline"
```

---

### Task 3: Alignment validity guard and diagnostics

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Modify: `cadscene/cli/align_to_cad.py`
- Test: `tests/alignment/test_aligner.py`
- Test: `tests/cli/test_align_to_cad_cli.py`

**Interfaces:**
- Consumes: alignment metrics, correspondences, trajectory intrinsics, selected FOV source.
- Produces: validation fields in `alignment.json` and a raised `RuntimeError` before artifact writes for invalid positional alignment.

- [ ] **Step 1: Write failing validation tests**

Add tests for:

- a deliberately invalid Sim3 with `global_residual_m_max > 5.0` being rejected;
- pathological `fx/fy > 2.0` producing an intrinsics warning while a consistent manual FOV permits alignment;
- pathological intrinsics with no trusted manual/config FOV being rejected.

```python
def test_alignment_rejects_global_anchor_residual_above_five_metres() -> None:
    with pytest.raises(RuntimeError, match="global anchor residual"):
        _validate_alignment_result(
            metrics={"global_residual_m_max": 54.15},
            intrinsics_warning=None,
            trusted_fov=True,
        )
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
pytest tests/alignment/test_aligner.py tests/cli/test_align_to_cad_cli.py -k "rejects_global_anchor or pathological_intrinsics" -v
```

Expected: FAIL because no validation helper or artifact-write guard exists.

- [ ] **Step 3: Implement minimal guards**

Add:

```python
MAX_GLOBAL_ANCHOR_RESIDUAL_M = 5.0
MAX_FOCAL_ASPECT_RATIO = 2.0
```

Record `fov_source`, `intrinsics_warning`, and validation status in alignment JSON. Raise before returning `AlignmentResult` when position residual exceeds 5 m or when trajectory intrinsics are pathological and no trusted manual/config FOV exists. Keep pathological-intrinsics alignment usable with an explicit warning when 67° is supplied by confirmed manual anchors.

- [ ] **Step 4: Run focused and CLI tests**

Run:

```powershell
pytest tests/alignment/test_aligner.py tests/cli/test_align_to_cad_cli.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit only Task 3 files**

```powershell
git add cadscene/alignment/aligner.py cadscene/cli/align_to_cad.py tests/alignment/test_aligner.py tests/cli/test_align_to_cad_cli.py
git commit -m "fix: reject misleading sfm cad alignment outputs"
```

---

### Task 4: Regression verification and current-run regeneration

**Files:**
- Regenerate ignored run artifacts under `runs/dataset-28cf432b-b798-4c26-b0c9-fe561014a989/cuda-bounded-full-20260724b/03_alignment`
- Regenerate ignored viewer scene under the same run's `05_viewer_scene`

**Interfaces:**
- Consumes: current `02_sfm`, `01_keyframes/camera_track_manual.json`, dataset manifest, fixed code.
- Produces: corrected alignment and viewer scene artifacts.

- [ ] **Step 1: Run the focused test suite**

```powershell
pytest tests/alignment tests/sfm/test_camera_init.py tests/viewer/test_export_scene.py tests/cli/test_align_to_cad_cli.py -v
```

Expected: PASS.

- [ ] **Step 2: Run the broader relevant suite**

```powershell
pytest tests/workflow/test_job_runner.py tests/viewer/test_web_viewer_static.py tests/viewer/test_workflow_ui_static.py -v
```

Expected: PASS.

- [ ] **Step 3: Rerun only alignment and viewer-scene stages**

Use the existing pipeline command for dataset `dataset-28cf432b-b798-4c26-b0c9-fe561014a989`, run `cuda-bounded-full-20260724b`, stages `alignment,viewer_scene`, existing trajectory, sparse point cloud, manual track, CAD directory, `cad_scale=1.0`, and `origin_xy=(470261.4895268574, 3188770.614712599)`. Do not include the SfM stage.

- [ ] **Step 4: Verify regenerated artifacts numerically**

Assert:

```text
alignment.metrics.global_residual_m_max < 0.01 m
mapped two-anchor baseline z difference < 0.01 m
all sfm_camera_path.csv fov values == 67.0
all algorithm_prediction FOV values == 67.0
viewer scene point/global-track transform == alignment Sim3
```

Also calculate and report the local point-cloud plane tilt. Do not call the point cloud coplanar if the tilt remains material.

- [ ] **Step 5: Verify in the running frontend**

Reload the existing viewer URL and confirm:

- the alignment stage loads successfully;
- route and global track no longer separate because of the former 105.35 m baseline height error;
- displayed camera FOV remains 67° across predicted frames;
- a warning is visible or reported for pathological upstream SfM geometry.

- [ ] **Step 6: Run final diff and status checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; unrelated existing changes remain untouched.
