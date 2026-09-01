# SRT Full-Pose CAD Georeference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use test-driven-development for every production change and verification-before-completion before reporting success.

**Goal:** Enable the existing `srt_full_pose` route so DJI SRT position/attitude plus a user-entered horizontal FOV produces a CAD-aligned metric camera trajectory without running SfM.

**Architecture:** Add a PROJ-backed CGCS2000 georeference layer with explainable CRS candidates and explicit confirmation. Store project-wide CAD georeference separately from per-clip full-pose settings, synchronize SRT against authoritative frame PTS, emit a trajectory already expressed in the existing local CAD metre frame, and teach workbench/render orchestration to consume a trajectory without sparse SfM geometry. Metric alignment is scale-locked and only permits explicit correction offsets.

**Tech Stack:** Python 3.10+, NumPy, SciPy Rotation, pyproj/PROJ, existing JSON repositories and ProjectService queue, vanilla JavaScript project workspace/viewer, pytest.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench\.worktrees\srt-full-pose-cad-georeference` on `codex/srt-full-pose-cad-georeference`; do not merge main during implementation.
- Preserve the approved design in `docs/superpowers/specs/2026-09-01-srt-full-pose-cad-georeference-design.md`.
- `horizontal_fov_deg` is one user-entered horizontal angle; do not add FOV type selection.
- Never hardcode central meridian 120° as a project-independent truth. EPSG:4549 may be the top candidate only when current inputs support it.
- Full-pose adapter and downstream commands must never invoke `cadscene.cli.run_sfm`.
- Trajectory `center` uses existing local CAD metre coordinates `(cad_raw_xy - origin_xy) * cad_scale`; diagnostics retain raw projected values.
- Metric scale remains exactly 1.0. No unconstrained Sim3 fallback is allowed.
- Horizontal CRS confirmation does not claim that CAD and SRT height datums match.
- Exact source integer PTS and frame maps remain authoritative; no FPS-derived mapping unless the existing synchronization contract explicitly confirms CFR.
- Existing `sfm_only`, `srt_sfm_fused` and `pure_rotation` behavior must remain unchanged.

---

### Task 1: PROJ-backed CGCS2000 georeference core

**Files:**
- Create: `cadscene/srt/georeference.py`
- Create: `tests/srt/test_georeference.py`
- Modify: `pyproject.toml`
- Modify: `tests/packaging/test_project_metadata.py`

**Interfaces:**
- Produces `CadGeoreference` with strict `from_dict()`/`to_dict()` validation.
- Produces `CrsCandidate` with EPSG, zone width, central meridian, CAD axis mapping, score and evidence.
- Produces `recommend_cgcs2000_candidates(longitudes, latitudes, cad_bbox_raw, *, limit=6) -> tuple[CrsCandidate, ...]`; returned tuples are immutable and deterministically sorted.
- Produces `project_wgs84_to_cad_raw(longitude, latitude, config) -> tuple[float, float]`.
- Produces `cad_raw_to_local_m(cad_xy, origin_xy, cad_scale) -> tuple[float, float]`.

- [ ] **Step 1: Write failing configuration and EPSG:4549 tests**

```python
def test_confirmed_epsg_4549_projects_with_explicit_axis_mapping():
    cfg = CadGeoreference.from_dict(_confirmed_4549())
    cad_x, cad_y = project_wgs84_to_cad_raw(120.0, 30.0, cfg)
    assert cad_x == pytest.approx(500000.0, abs=0.01)
    assert cad_y > 3_000_000

def test_unconfirmed_config_cannot_project():
    with pytest.raises(ValueError, match="confirmed"):
        project_wgs84_to_cad_raw(120.0, 30.0, _config(confirmed=False))

def test_axis_swap_is_explicit_not_inherited_from_epsg_axis_order():
    east_north = project_wgs84_to_cad_raw(120.0, 30.0, _config(mapping="cad_x_easting_cad_y_northing"))
    north_east = project_wgs84_to_cad_raw(120.0, 30.0, _config(mapping="cad_x_northing_cad_y_easting"))
    assert north_east == pytest.approx(tuple(reversed(east_north)))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -q`

Expected: collection fails because `cadscene.srt.georeference` does not exist.

- [ ] **Step 3: Implement strict config and transformer cache**

Use `pyproj.CRS.from_epsg()` and `Transformer.from_crs("EPSG:4326", target, always_xy=True)`. Validate datum name, projected CRS, metres, finite central meridian, supported axis mapping and EPSG/declared parameter consistency. Raise a dedicated environment/configuration error when PROJ data is unavailable.

- [ ] **Step 4: Write failing candidate tests**

```python
def test_candidate_near_120_recommends_4549_when_bbox_matches():
    candidates = recommend_cgcs2000_candidates(
        [119.99, 120.01], [30.0, 30.001],
        cad_bbox_raw=(499000.0, 3319000.0, 501000.0, 3321000.0),
    )
    assert candidates[0].epsg == 4549

def test_other_region_does_not_reuse_120_degree_candidate():
    candidates = recommend_cgcs2000_candidates(
        [117.0, 117.01], [31.0, 31.01],
        cad_bbox_raw=_matching_bbox_for_117e(),
    )
    assert candidates[0].central_meridian_deg == 117.0
    assert candidates[0].epsg != 4549

def test_ambiguous_axis_candidates_remain_unconfirmed():
    candidates = recommend_cgcs2000_candidates(
        [120.0, 120.001], [30.0, 30.001],
        cad_bbox_raw=(0.0, 0.0, 4_000_000.0, 4_000_000.0),
    )
    assert candidates[0].confirmed is False
    assert "axis" in candidates[0].evidence
```

- [ ] **Step 5: Implement candidate discovery and explainable bbox scoring**

Query the local EPSG database for CGCS2000 projected CRSs around the SRT median coordinate, filter to Gauss-Kruger 3°/6° variants, add both supported CAD axis mappings, project sampled positions, then score valid ratio, inside/buffered-bbox ratio, bbox distance, unit and zone-prefix evidence. Sort deterministically; never set `confirmed=True` in the recommender.

- [ ] **Step 6: Declare and verify the dependency**

Add `pyproj>=3.6` to project dependencies and assert it in `tests/packaging/test_project_metadata.py`.

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py tests/packaging/test_project_metadata.py -q`

Expected: PASS.

Commit: `git add cadscene/srt/georeference.py tests/srt/test_georeference.py pyproject.toml tests/packaging/test_project_metadata.py && git commit -m "feat: add CGCS2000 georeference candidates"`

### Task 2: Full attitude synchronization and camera math

**Files:**
- Modify: `cadscene/srt/synchronization.py`
- Create: `cadscene/srt/full_pose.py`
- Modify: `cadscene/srt/__init__.py`
- Modify: `cadscene/core/camera.py`
- Modify: `cadscene/alignment/aligner.py`
- Create: `tests/srt/test_full_pose.py`
- Modify: `tests/srt/test_synchronization.py`
- Modify: `tests/alignment/test_aligner.py`

**Interfaces:**
- `FrameSrtSample` adds gimbal/drone yaw, pitch and roll without changing existing positional fields.
- Angular interpolation follows the shortest wrapped path and remains bounded by the existing maximum gap.
- Produces `horizontal_fov_intrinsics(width, height, horizontal_fov_deg) -> dict`.
- Produces `dji_ned_gimbal_to_cam_from_world_quat(yaw, pitch, roll, *, profile) -> list[float]`.
- Moves the existing camera/world rotation conversion into a public core helper shared by alignment and full-pose code.

- [ ] **Step 1: Write failing SRT attitude interpolation tests**

```python
def test_gimbal_attitude_is_sampled_with_position():
    sample = sample_srt_at_frames(_records_with_attitude(), frame_timestamps=[_frame(0.5)], max_interpolation_gap_sec=1.0)[0]
    assert sample.gimbal_pitch == pytest.approx(-45.0)
    assert sample.gimbal_roll == pytest.approx(2.0)

def test_yaw_interpolation_wraps_across_180_on_short_arc():
    sample = sample_srt_at_frames(_yaw_records(179.0, -179.0), frame_timestamps=[_frame(0.5)], max_interpolation_gap_sec=1.0)[0]
    assert abs(abs(sample.gimbal_yaw) - 180.0) < 1e-6

def test_long_gap_does_not_interpolate_attitude():
    sample = sample_srt_at_frames(
        _yaw_records_at_times((0.0, 179.0), (10.0, -179.0)),
        frame_timestamps=[_frame(5.0)],
        max_interpolation_gap_sec=1.0,
    )[0]
    assert sample.gimbal_yaw is None
```

- [ ] **Step 2: Run synchronization tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_synchronization.py -q`

Expected: attitude attributes are missing.

- [ ] **Step 3: Extend samples and bounded circular interpolation**

Keep current GPS/height behavior byte-for-byte compatible. Add optional attitude fields at the end of the dataclass and interpolate only when both bounding records contain finite values.

- [ ] **Step 4: Write failing horizontal FOV and pose convention tests**

```python
def test_horizontal_fov_produces_square_pixel_pinhole():
    intrinsics = horizontal_fov_intrinsics(3840, 2160, 90.0)
    assert intrinsics["params"] == pytest.approx([1920.0, 1920.0, 1920.0, 1080.0])

@pytest.mark.parametrize("yaw, expected_forward", [(0, [0, 1, 0]), (90, [1, 0, 0])])
def test_dji_yaw_zero_is_north_and_positive_clockwise(yaw, expected_forward):
    rotation = camera_to_world_from_cam_quat(dji_ned_gimbal_to_cam_from_world_quat(yaw, 0, 0, profile="dji_absolute_ned"))
    assert rotation[:, 2] == pytest.approx(expected_forward)

def test_negative_dji_pitch_points_camera_downward():
    quat = dji_ned_gimbal_to_cam_from_world_quat(0.0, -45.0, 0.0, profile="dji_absolute_ned")
    rotation = camera_to_world_from_cam_quat(quat)
    assert rotation[2, 2] < 0
```

- [ ] **Step 5: Implement and centralize camera rotation conventions**

Use the existing project convention as the reference, convert DJI negative-down gimbal pitch into the backend positive-down `CameraState.pitch_deg`, form camera-to-world, transpose to the schema's `cam_from_world`, and serialize a normalized wxyz quaternion. Add finite/range validation and the explicit profile name.

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_synchronization.py tests/srt/test_full_pose.py tests/alignment/test_aligner.py -q`

Expected: PASS with legacy alignment convention tests unchanged.

Commit: `git add cadscene/srt/synchronization.py cadscene/srt/full_pose.py cadscene/srt/__init__.py cadscene/core/camera.py cadscene/alignment/aligner.py tests/srt/test_synchronization.py tests/srt/test_full_pose.py tests/alignment/test_aligner.py && git commit -m "feat: convert DJI SRT attitude to camera poses"`

### Task 3: Atomic full-pose trajectory builder and CLI

**Files:**
- Modify: `cadscene/srt/parser.py`
- Modify: `cadscene/srt/full_pose.py`
- Create: `cadscene/cli/build_srt_full_pose.py`
- Create: `tests/cli/test_build_srt_full_pose_cli.py`
- Modify: `tests/srt/test_parser.py`
- Modify: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- Produces public `load_srt_records(path) -> list[SrtRecord]` without removing incremental size bounds.
- Produces `FullPoseBuildConfig` and `build_full_pose_trajectory(records, frame_map, video_metadata, config, output_directory) -> FullPoseBuildResult`.
- CLI accepts video/SRT/frame map, clip id, source interval, confirmed georeference JSON, CAD origin/scale, FOV, height offset/profile and output root.
- Writes `02_srt_full_pose/camera_trajectory_full_pose.json`, CSV path, diagnostics JSON and Markdown report atomically.

- [ ] **Step 1: Write failing public parser and trajectory-builder tests**

```python
def test_public_loader_retains_gimbal_fields(tmp_path):
    records = load_srt_records(_write_dji_srt(tmp_path))
    assert records[0].gimbal_yaw == 90.0

def test_builder_emits_local_cad_metric_centers_and_user_fov(tmp_path):
    result = build_full_pose_trajectory(_fixture_inputs(tmp_path, epsg=4549, fov=82.0))
    payload = json.loads(result.trajectory_path.read_text())
    assert payload["meta"]["trajectory_mode"] == "srt_full_pose"
    assert payload["meta"]["coordinate_system"] == "cad_local_m"
    assert payload["meta"]["metric_scale_locked"] is True
    assert payload["meta"]["fov_source"] == "user"
    assert payload["intrinsics"][0]["horizontal_fov_deg"] == 82.0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_parser.py tests/cli/test_build_srt_full_pose_cli.py -q`

Expected: public loader and CLI are missing.

- [ ] **Step 3: Implement exact frame-map loading and trajectory construction**

Read the authoritative clip entry from the frame map, convert source PTS to seconds using its exact time base, then reuse `sample_srt_at_frames`. Registered poses require valid GPS, height and complete gimbal attitude; long gaps emit `registered=false` rows rather than extrapolation. Select relative height first, apply explicit `cad_z_offset_m`, and record height warnings.

- [ ] **Step 4: Add diagnostics rejection tests**

Cover invalid speed, GPS jump, pose coverage below 80%, unconfirmed CRS, interval/frame-map mismatch, missing video dimensions and non-finite FOV. Ensure failures publish no partial active trajectory.

- [ ] **Step 5: Implement CLI atomic publication and reports**

Write into a temporary directory under the attempt root, validate the complete artifact set, then replace final files. Diagnostics contain CRS evidence, inside-bbox ratio, position/attitude coverage, speed/jump summaries, warnings and the input fingerprint.

- [ ] **Step 6: Verify wheel inclusion and commit**

Run: `python -m pytest -p no:cacheprovider tests/srt tests/cli/test_build_srt_full_pose_cli.py tests/packaging/test_wheel_contents.py -q`

Expected: PASS.

Commit: `git add cadscene/srt/parser.py cadscene/srt/full_pose.py cadscene/cli/build_srt_full_pose.py tests/srt tests/cli/test_build_srt_full_pose_cli.py tests/packaging/test_wheel_contents.py && git commit -m "feat: build full-pose SRT trajectories"`

### Task 4: Enable the reserved workflow adapter

**Files:**
- Modify: `cadscene/projects/adapters.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/projects/service.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Modify: `tests/projects/test_executor.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- `AdapterInputs` adds optional CAD georeference/config paths or equivalent immutable parameters needed by full pose.
- `srt_full_pose` adapter becomes available, version 2, module `cadscene.cli.build_srt_full_pose` and output `02_srt_full_pose/camera_trajectory_full_pose.json`.
- Full-pose input fingerprint includes CAD asset version, confirmed georeference revision, exact PTS/frame map, SRT/video identity, FOV, height offset and attitude profile.

- [ ] **Step 1: Write failing adapter contract tests**

```python
def test_full_pose_adapter_is_available_and_runs_no_sfm(registry, inputs):
    adapter = registry.for_workflow("srt_full_pose")
    commands = adapter.build_commands(_full_pose_inputs(inputs))
    assert adapter.available is True
    assert len(commands) == 1
    assert "cadscene.cli.build_srt_full_pose" in commands[0]
    assert all("cadscene.cli.run_sfm" not in token for command in commands for token in command)

def test_full_pose_prepare_requires_confirmed_crs_fov_and_exact_map(inputs):
    with pytest.raises(ValueError, match="horizontal_fov_deg"):
        adapter.prepare_inputs(_full_pose_inputs_without_fov())
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workflow_adapters.py tests/projects/test_executor.py -k full_pose -q`

Expected: adapter remains interface-only.

- [ ] **Step 3: Wire command, output validation and proof**

Add the module to the adapter's closed dispatch, pass configuration through a JSON file inside the attempt rather than an oversized command line, validate meta workflow/CRS/scale lock and hash trajectory, diagnostics and frame map into validation proof.

- [ ] **Step 4: Integrate ProjectService preparation and stale identity**

Merge project-wide confirmed georeference with `clip.manual_definition["srt_full_pose"]`, materialize it into the immutable attempt, and produce explicit preflight blockers for missing/invalid configuration. Configuration revision changes must make previous attempts non-current.

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workflow_adapters.py tests/projects/test_executor.py tests/projects/test_workbench_sessions.py -k "full_pose or trajectory_preflight" -q`

Expected: PASS.

Commit: `git add cadscene/projects/adapters.py cadscene/projects/workflow_adapters.py cadscene/projects/service.py tests/projects/test_workflow_adapters.py tests/projects/test_executor.py tests/projects/test_workbench_sessions.py && git commit -m "feat: enable SRT full-pose adapter"`

### Task 5: Versioned georeference and FOV project APIs

**Files:**
- Modify: `cadscene/projects/models.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `tests/projects/test_json_repositories.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/projects/test_http_api.py` if present, otherwise the nearest API contract test module.

**Interfaces:**
- Project user config stores `_cad_georeference` with its own revision, CAD asset fingerprint and confirmation source.
- Per-clip `manual_definition["srt_full_pose"]` stores `horizontal_fov_deg`, `cad_z_offset_m` and `attitude_profile`.
- Produces service methods `recommend_cad_georeference`, `confirm_cad_georeference`, and `update_srt_full_pose_settings` with optimistic revision checks.
- Adds project API routes for candidate generation/confirmation and clip full-pose settings.

- [ ] **Step 1: Write failing persistence and revision tests**

```python
def test_georeference_confirmation_is_project_wide_and_cad_version_bound(service):
    project = service.repositories.project.load("p1")
    confirmed = service.confirm_cad_georeference(
        "p1", _candidate_4549(), expected_project_revision=project.revision,
    )
    assert confirmed.source_assets["_cad_georeference"]["epsg"] == 4549
    replace_cad(service, "p1")
    assert service.snapshot("p1")["cad_georeference"]["confirmed"] is False

def test_fov_is_per_clip_and_survives_analysis_reconciliation(service):
    clips = service.repositories.clips.load("p1")
    service.update_srt_full_pose_settings(
        "p1", "clip-1", horizontal_fov_deg=82.0,
        cad_z_offset_m=0.0, attitude_profile="dji_absolute_ned",
        expected_clips_revision=clips.revision,
    )
    rerun_analysis_and_activate(service)
    assert clip("clip-1").manual_definition["srt_full_pose"]["horizontal_fov_deg"] == 82.0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_json_repositories.py tests/projects/test_workbench_sessions.py -k "georeference or full_pose_settings" -q`

Expected: service/API methods are missing.

- [ ] **Step 3: Implement immutable user-owned configuration updates**

Use existing repository mutation and `RevisionConflict`. Validate all fields through the core dataclasses, bind georeference to the active CAD fingerprint, and preserve `manual_definition` through clip reconciliation.

- [ ] **Step 4: Add snapshot and preflight payloads**

Expose candidates and evidence only for projects with SRT GPS. Full-pose rows return the current horizontal FOV and concise blockers such as “请确认 CAD 坐标系” or “请输入水平视场角”. Avoid parsing entire SRT on every polling snapshot; cache candidates by CAD/SRT fingerprint or generate through the explicit endpoint.

- [ ] **Step 5: Run API/repository tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects -k "georeference or full_pose or workflow_override or reconcile" -q`

Expected: PASS.

Commit: `git add cadscene/projects/models.py cadscene/projects/service.py cadscene/projects/http_api.py tests/projects && git commit -m "feat: persist CAD georeference and drone FOV"`

### Task 6: Project workspace confirmation UI

**Files:**
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/project_workspace.css`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Preserve all four workflow names in `visibleWorkflowChoice`; do not collapse SRT routes to `sfm_only`.
- Full-pose row/panel displays horizontal FOV input, recommended CRS, central meridian, EPSG, axis mapping, score evidence, confirm/edit actions and preflight blockers.
- Candidate preview is a lightweight CAD bbox plus sampled trajectory polyline with start/end markers.

- [ ] **Step 1: Write failing static UI contract tests**

```python
def test_workspace_does_not_collapse_srt_full_pose_to_sfm_only():
    script = _workspace_script()
    assert 'workflow === "pure_rotation" ? "pure_rotation" : "sfm_only"' not in script

def test_full_pose_form_labels_fov_as_horizontal_and_has_no_type_selector():
    html = _workspace_html()
    assert "水平视场角（°）" in html
    assert "fovType" not in html

def test_crs_confirmation_posts_epsg_axis_and_revision():
    script = _workspace_script()
    assert "/cad-georeference/confirm" in script
    assert "cad_axis_mapping" in script
    assert "project_revision" in script
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -q`

Expected: full-pose controls do not exist and workflow is collapsed.

- [ ] **Step 3: Implement form state and optimistic API updates**

Disable trajectory enqueue until FOV and CRS are valid. Use the existing snapshot revision refresh/retry pattern; never mutate configuration optimistically without server confirmation.

- [ ] **Step 4: Implement candidate evidence and preview**

Render candidates deterministically, default-select only the top recommendation, and require an explicit confirmation click. Display “120°是本项目推荐中央经线，不会应用到其他 CAD” when EPSG:4549 is selected.

- [ ] **Step 5: Run UI/service tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py tests/projects/test_workbench_sessions.py -k "full_pose or georeference or workspace" -q`

Expected: PASS.

Commit: `git add apps/project_workspace tests/viewer/test_project_workspace_static.py tests/projects/test_workbench_sessions.py && git commit -m "feat: configure full-pose SRT projects in workspace"`

### Task 7: Scale-locked alignment and no-pointcloud downstream

**Files:**
- Modify: `cadscene/alignment/aligner.py`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `cadscene/viewer/export_scene.py`
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/web_camera_viewer/workflow.js`
- Create: `configs/pipelines/srt_full_pose_overlay.yaml`
- Modify: `tests/alignment/test_aligner.py`
- Modify: `tests/viewer/test_export_scene.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Alignment recognizes trajectory meta `coordinate_system=cad_local_m` and `metric_scale_locked=true`.
- Direct mode uses identity rotation/scale and optional explicit XYZ translation plus attitude zero offsets; it never estimates free scale.
- Workflow inputs resolve adapter output key `trajectory` and optional `sparse_points`, not fixed `02_sfm` paths.
- Viewer scene and quality/render stages accept an empty point cloud for full pose and skip road-surface analysis that requires sparse geometry.

- [ ] **Step 1: Write failing scale-lock tests**

```python
def test_metric_full_pose_alignment_keeps_scale_one_with_manual_anchor(tmp_path):
    result = run_alignment(
        trajectory_path=_metric_full_pose_trajectory(tmp_path),
        camera_track_path=_one_shifted_anchor(tmp_path),
        output_dir=tmp_path / "alignment",
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )
    assert result.alignment_json["sim3"]["scale"] == 1.0
    assert result.alignment_json["validation"]["alignment_mode"] == "metric_direct"
    assert result.alignment_json["validation"]["metric_scale_locked"] is True

def test_metric_full_pose_never_calls_free_sim3(monkeypatch):
    monkeypatch.setattr(aligner, "estimate_global_sim3", _fail)
    run_alignment(
        trajectory_path=_metric_full_pose_trajectory(),
        camera_track_path=_one_shifted_anchor(),
        output_dir=_output_dir(),
        config=AlignmentConfig(),
    )
```

- [ ] **Step 2: Run alignment tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/alignment/test_aligner.py -k metric_full_pose -q`

Expected: current aligner estimates Sim3 or rejects one anchor.

- [ ] **Step 3: Implement explicit metric-direct mode**

Use trajectory meta to select the mode. Without correction anchors, publish the direct path. With anchors, compute a robust fixed translation and independent wrapped yaw/pitch/roll offsets; report residuals and reject inconsistent corrections. Keep legacy SfM behavior unchanged.

- [ ] **Step 4: Write failing generic artifact/no-PLY tests**

```python
def test_full_pose_workbench_uses_adapter_trajectory_not_02_sfm(service):
    session = service.prepare_workbench(_successful_full_pose_job())
    assert "02_srt_full_pose" in session.trajectory_path.as_posix()

def test_full_pose_viewer_exports_empty_points_without_sparse_ply(tmp_path):
    scene = _export_full_pose_scene(tmp_path, sparse_ply=None)
    assert scene["point_cloud"]["count_exported"] == 0

def test_full_pose_pipeline_contains_no_road_surface_or_sfm_requirement():
    pipeline = yaml.safe_load(Path("configs/pipelines/srt_full_pose_overlay.yaml").read_text())
    assert "road_surface" not in pipeline["stages"]
    assert "sparse_ply" not in json.dumps(pipeline)
```

- [ ] **Step 5: Decouple workbench/job runner from fixed SfM files**

Resolve current trajectory job outputs by semantic key, thread the optional sparse path through stage resolution, and use the full-pose pipeline configuration. Update browser workflow initialization to preserve `srt_full_pose`, load its trajectory FOV/poses and display an empty point cloud without an error.

- [ ] **Step 6: Run focused integration tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/alignment/test_aligner.py tests/viewer/test_export_scene.py tests/projects/test_workbench_sessions.py tests/viewer/test_workflow_ui_static.py -k "full_pose or metric_direct or optional_sparse" -q`

Expected: PASS.

Commit: `git add cadscene/alignment/aligner.py cadscene/workflow/job_runner.py cadscene/viewer/export_scene.py cadscene/projects/workbench_sessions.py cadscene/projects/http_api.py apps/web_camera_viewer/workflow.js configs/pipelines/srt_full_pose_overlay.yaml tests/alignment/test_aligner.py tests/viewer/test_export_scene.py tests/projects/test_workbench_sessions.py tests/viewer/test_workflow_ui_static.py && git commit -m "feat: run full-pose workflow without sparse SfM"`

### Task 8: End-to-end, offline packaging and documentation verification

**Files:**
- Modify: `tests/integration/test_srt_full_pose_workflow.py`
- Modify: `tests/packaging/test_windows_offline_bundle.py`
- Modify: `scripts/windows_offline_bundle.py` only if PROJ data is not already retained by conda-pack.
- Modify: `README.md`
- Modify: `docs/workflow_routing.md`
- Modify: `docs/technical/developer-guide.md`
- Modify: `docs/technical/troubleshooting.md`

**Interfaces:**
- Synthetic end-to-end fixture uploads/constructs CAD bbox, video metadata, full-pose SRT and exact frame map; confirms CRS/FOV; runs the adapter; opens workbench; produces a no-PLY scene.
- Offline verifier imports pyproj, resolves EPSG:4549 and transforms a known point without repository checkout access.

- [ ] **Step 1: Add failing end-to-end test**

```python
def test_srt_full_pose_project_runs_without_sfm_and_opens_workbench(tmp_path):
    project = _full_pose_project(tmp_path)
    confirm_crs(project, epsg=4549, horizontal_fov_deg=82.0)
    result = run_trajectory_job(project)
    assert result.status == "success"
    assert "run_sfm" not in result.recorded_modules
    scene = open_workbench(project)
    assert scene["meta"]["workflow"] == "srt_full_pose"
    assert scene["point_cloud"]["count_exported"] == 0
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/integration/test_srt_full_pose_workflow.py -q`

Expected: missing end-to-end wiring is reported.

- [ ] **Step 3: Complete integration and offline PROJ smoke**

Build a wheel, install/extract it away from the checkout, import `pyproj`, resolve EPSG:4549 and verify `(120°, 30°)` projects near easting 500000 m. If the offline bundle omits PROJ data, include it through the existing conda-pack verification path rather than copying from the developer machine ad hoc.

- [ ] **Step 4: Update user and developer documentation**

Document that 120° is a per-project central-meridian candidate, FOV means horizontal angle, SRT full pose skips SfM, height needs separate confirmation, and coordinate confirmation is mandatory.

- [ ] **Step 5: Run focused suites**

Run: `python -m pytest -p no:cacheprovider tests/srt tests/integration/test_srt_full_pose_workflow.py tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/alignment/test_aligner.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py tests/packaging/test_project_metadata.py tests/packaging/test_wheel_contents.py tests/packaging/test_windows_offline_bundle.py -q`

Expected: PASS.

- [ ] **Step 6: Run full verification**

Run: `python -m pytest -p no:cacheprovider`

Expected: all tests pass with only established skips.

Run: `python scripts/check_no_project_dependency.py`

Expected: success.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Run isolated real-input smoke when assets are available**

Use a writable disposable project copy while referencing user CAD/video/SRT assets read-only. Confirm the recommended CRS (expected current candidate EPSG:4549/120° only if the data supports it), enter the supplied horizontal FOV, run one full-pose trajectory attempt, verify no SfM command appears, inspect sampled start/end locations and four pose frustums, and record height/attitude warnings. Never edit the source assets.

- [ ] **Step 8: Commit final integration and docs**

Commit: `git add tests/integration/test_srt_full_pose_workflow.py tests/packaging/test_windows_offline_bundle.py scripts/windows_offline_bundle.py README.md docs/workflow_routing.md docs/technical/developer-guide.md docs/technical/troubleshooting.md && git commit -m "test: verify SRT full-pose workflow end to end"`

## Self-review Checklist

- [ ] Every production change is preceded by a failing test.
- [ ] No placeholder/TODO/deferred error behavior remains.
- [ ] 120° appears only as current-input evidence or a test fixture, never as the universal default.
- [ ] All public FOV copy says horizontal and stores a single numeric value.
- [ ] Full-pose command audit contains no SfM module.
- [ ] Metric scale remains 1.0 through trajectory, alignment, workbench and render.
- [ ] Raw projected coordinates, local CAD metres and Viewer CAD world are not conflated.
- [ ] Horizontal and vertical confidence/warnings are reported separately.
- [ ] Old workflow tests remain green.
- [ ] Wheel/offline package can load PROJ data away from the checkout.
