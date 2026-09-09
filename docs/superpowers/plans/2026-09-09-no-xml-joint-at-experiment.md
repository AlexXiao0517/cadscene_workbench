# No-XML Joint AT Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce three renderer-ready probe frames from a jointly aligned COLMAP/SRT/DJI trajectory without allowing Bentley XML into the solver or renderer.

**Architecture:** Keep the existing COLMAP sparse model fixed and solve a compact robust least-squares problem for one shared world Sim3, one bounded DJI-to-camera installation rotation, and low-frequency XYZ correction knots. Emit the existing `camera_trajectory.json` and `camera_calibration.json` contracts so the existing experimental CAD/TPKG renderer can consume the result unchanged; run XML only afterward in a separate comparison command.

**Tech Stack:** Python 3.11, NumPy, SciPy `least_squares`/`Rotation`/`Slerp`, existing CADScene SRT/georeference helpers, pytest, OpenCV probe renderer.

## Global Constraints

- Solver and renderer command-line interfaces must not accept an XML path.
- COLMAP centers and rotations must share one global rotation `G`.
- DJI attitude must use Bentley `XRightYDown` matrix composition.
- Installation correction is constant for the whole video and bounded to ±6° per rotation-vector component.
- Low-frequency XYZ knots are spaced every 20 seconds and regularized by their second difference and endpoints.
- SRT absolute height is preserved; no fixed vertical offset is invented.
- Probe confirmation is experimental only and must not become a formal product task state.
- Do not rerun COLMAP feature extraction, matching, or mapping.

---

### Task 1: Joint alignment solver

**Files:**
- Create: `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/experiments/at-replication/joint_at_alignment.py`
- Create: `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py`

**Interfaces:**
- Consumes: `frames: ndarray[N]`, COLMAP `centers: ndarray[N,3]`, COLMAP `world_from_camera: ndarray[N,3,3]`, projected SRT centers, and DJI aircraft/gimbal values.
- Produces: `bentley_world_from_camera(yaw_deg, pitch_deg, roll_deg) -> ndarray[3,3]` and `solve_joint_alignment(...) -> JointAlignmentResult` with one `world_rotation`, `scale`, `translation`, `installation_rotation`, corrected sparse centers/rotations, diagnostics, and convergence status.

- [ ] **Step 1: Write failing Bentley-matrix tests**

```python
def test_bentley_world_from_camera_matches_xright_ydown_formula():
    actual = bentley_world_from_camera(-94.4, -45.1, 0.0)
    expected = bentley_ypr_world_to_camera_rotation(
        yaw_deg=-94.4, pitch_deg=-45.1, roll_deg=0.0
    ).T
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    np.testing.assert_allclose(actual.T @ actual, np.eye(3), atol=1e-12)
    assert np.linalg.det(actual) == pytest.approx(1.0)
```

- [ ] **Step 2: Run the matrix test and verify RED**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py::test_bentley_world_from_camera_matches_xright_ydown_formula`

Expected: FAIL because `joint_at_alignment.py` or `bentley_world_from_camera` does not exist.

- [ ] **Step 3: Implement the exact Bentley matrix conversion**

```python
def bentley_world_from_camera(yaw_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    yaw, pitch, roll = np.radians([yaw_deg, pitch_deg, roll_deg])
    cy, sy, cp, sp, cr, sr = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch), np.cos(roll), np.sin(roll)
    world_to_camera = np.asarray([
        [cr * cy - sr * sp * sy, -cr * sy - cy * sr * sp, cp * sr],
        [cy * sr + cr * sp * sy, cr * cy * sp - sr * sy, -cr * cp],
        [cp * sy, cp * cy, sp],
    ])
    return world_to_camera.T
```

- [ ] **Step 4: Run the matrix test and verify GREEN**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py::test_bentley_world_from_camera_matches_xright_ydown_formula`

Expected: `1 passed`.

- [ ] **Step 5: Write a failing synthetic recovery test**

```python
def test_joint_solver_recovers_shared_world_rotation_and_rejects_split_gauge():
    frames, visual_centers, visual_rotations, srt_centers, dji_rotations, expected = synthetic_problem()
    result = solve_joint_alignment(
        frames=frames,
        colmap_centers=visual_centers,
        colmap_world_from_camera=visual_rotations,
        srt_centers=srt_centers,
        dji_world_from_camera=dji_rotations,
        frame_rate=60.0,
        knot_interval_sec=20.0,
    )
    assert result.success
    np.testing.assert_allclose(result.world_rotation, expected.world_rotation, atol=2e-3)
    np.testing.assert_allclose(result.installation_rotation, expected.installation_rotation, atol=2e-3)
    for source, solved in zip(visual_rotations, result.world_from_camera):
        np.testing.assert_allclose(solved, result.world_rotation @ source, atol=1e-10)
```

- [ ] **Step 6: Run the synthetic test and verify RED**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py::test_joint_solver_recovers_shared_world_rotation_and_rejects_split_gauge`

Expected: FAIL because `solve_joint_alignment` is not implemented.

- [ ] **Step 7: Implement robust joint least squares**

Implement a `JointAlignmentResult` dataclass and solve variables `[log_scale, world_rotvec(3), translation(3), install_rotvec(3), knots(K,3)]` with `scipy.optimize.least_squares(loss="soft_l1")`. Position residuals use `(s * G @ C_i + t + d_i - S_i) / 3.0`; orientation residuals use `Rotation.from_matrix((T_i @ B).T @ (G @ R_i)).as_rotvec() / radians(2.0)`; installation regularization uses `install_rotvec / radians(2.0)`; second differences use `(d[k-1] - 2*d[k] + d[k+1]) / 1.0`; endpoint knots use `d[[0,-1]] / 2.0`. Bound installation components to `±radians(6)` and reject results that finish within `0.05°` of a bound.

- [ ] **Step 8: Run all joint solver tests**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py`

Expected: all tests pass, including an outlier test where one SRT center is shifted 50 m and the recovered transform remains within the synthetic tolerances.

---

### Task 2: No-XML result builder

**Files:**
- Create: `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/experiments/at-replication/build_joint_backend_at_result.py`
- Modify: `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/experiments/at-replication/test_build_backend_at_result.py`

**Interfaces:**
- Consumes: existing COLMAP text model, MP4 `djmd`, SRT, central meridian, CAD origin, integer FOV.
- Produces: `camera_trajectory.json`, copied `camera_calibration.json`, and `joint_at_manifest.json` with `xml_used=false`, one shared rotation, objective diagnostics, knot corrections, and source provenance.

- [ ] **Step 1: Write the failing no-XML CLI contract test**

```python
def test_joint_builder_has_no_xml_argument():
    module = load_script("build_joint_backend_at_result.py")
    destinations = {action.dest for action in module.build_parser()._actions}
    assert "model" in destinations
    assert "video" in destinations
    assert "srt" in destinations
    assert "xml" not in destinations
```

- [ ] **Step 2: Run the contract test and verify RED**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_build_backend_at_result.py::test_joint_builder_has_no_xml_argument`

Expected: FAIL because the joint builder does not exist.

- [ ] **Step 3: Implement the builder by composing existing loaders with Task 1**

The builder must reuse `load_colmap_poses`, `parse_radial_camera`, `interpolate_pose_track`, SRT projection, and the observed `djmd` decoder. Construct DJI priors with `roll_deg=0.0` for the first pass because the recorded aircraft pitch contains transient flight dynamics rather than a fixed optical roll. Call `solve_joint_alignment`, interpolate corrected sparse poses to `range(len(srt_records))`, and serialize quaternions in `xyzw` order.

- [ ] **Step 4: Add and pass trajectory invariant tests**

```python
def test_joint_manifest_declares_one_shared_rotation(result_dir):
    manifest = json.loads((result_dir / "joint_at_manifest.json").read_text())
    assert manifest["solver_inputs"]["xml_used"] is False
    assert "orientation_global_rotation" not in manifest["transforms"]
    assert manifest["transforms"]["shared_world_rotation"]
    assert manifest["coverage"]["output_frames"] == 12017
```

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_build_backend_at_result.py`

Expected: all tests pass.

- [ ] **Step 5: Generate the frozen joint result**

Run the builder against `mavic4-1080p-0p5s/radial-grid-cx950p4-cy518p4/model-out-refined-text`, the original MP4/SRT, central meridian `118.83333333333333`, CAD origin `484717.5726936237 3189945.713859801`, and FOV `59`; write to `work/srt-full-pose-ui-test/experiments/at-replication/backend-at-joint-v1`.

Expected: `status=passed`, 401 registered sparse frames, 12,017 output frames, finite normalized quaternions, and a manifest with no XML path.

---

### Task 3: Probe render and frozen-answer comparison

**Files:**
- Modify: `D:/zjic2026/cadscene_workbench/work/tpkg-analysis/test_render_backend_at_preview.py`
- Modify: `D:/zjic2026/cadscene_workbench/work/tpkg-analysis/render_backend_at_preview.py`
- Create: `D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/experiments/at-replication/evaluate_frozen_joint_result.py`

**Interfaces:**
- Consumes: Task 2 trajectory/calibration, original video/CAD, both TPKG files; comparison command separately consumes the frozen result and XML.
- Produces: probe frames 300/6000/11400, a no-XML render report, and a `reference_only` XML comparison report that cannot mutate the frozen result.

- [ ] **Step 1: Write failing renderer provenance tests**

```python
def test_probe_report_records_joint_result_without_xml(tmp_path):
    report = build_report(
        trajectory=tmp_path / "camera_trajectory.json",
        calibration=tmp_path / "camera_calibration.json",
        output_dir=tmp_path,
    )
    assert report["xml_used"] is False
    assert report["pose_contract"] == "shared_world_sim3_plus_low_frequency_xyz"
```

- [ ] **Step 2: Run the provenance test and verify RED**

Run: `pytest -q work/tpkg-analysis/test_render_backend_at_preview.py::test_probe_report_records_joint_result_without_xml`

Expected: FAIL because `build_report` does not expose the joint pose contract.

- [ ] **Step 3: Implement report provenance and render three probes**

Run `render_backend_at_preview.py --probe-only` with Task 2 outputs, the original MP4, original CAD bundle, `zhix.tpkg`, and `station.tpkg`; write to `work/srt-full-pose-ui-test/manual-renders/backend-at-joint-v1`.

Expected: three 1920×1080 JPEG files with finite projection and original CAD colors.

- [ ] **Step 4: Freeze outputs before XML comparison**

Compute SHA-256 for `camera_trajectory.json`, `camera_calibration.json`, `joint_at_manifest.json`, and all three probe images. Store the hashes in `frozen_outputs.json`; the comparison script verifies the hashes before and after evaluation and fails if any change.

- [ ] **Step 5: Run the isolated XML comparison**

Run `evaluate_frozen_joint_result.py` with the frozen result directory and `C:/Users/Hanshark/Desktop/1080p Block - 0.5秒.xml`.

Expected: the report contains `reference_only=true`, repeats the frozen hashes, and reports total/low-frequency position and attitude error plus predicted screen displacement versus the previous no-XML baseline. It does not write into the frozen result directory.

- [ ] **Step 6: Run the complete experimental regression suite**

Run: `pytest -q work/srt-full-pose-ui-test/experiments/at-replication/test_joint_at_alignment.py work/srt-full-pose-ui-test/experiments/at-replication/test_build_backend_at_result.py work/tpkg-analysis/test_render_backend_at_preview.py work/tpkg-analysis/test_render_tpkg_xml_preview.py`

Expected: all tests pass without an XML dependency in the solver or renderer.

- [ ] **Step 7: Present probes for user confirmation**

Open the three probe images in Codex and report the no-XML objective changes and reference-only comparison. Do not start the full 5,575-frame render until the user confirms the experimental probes.
