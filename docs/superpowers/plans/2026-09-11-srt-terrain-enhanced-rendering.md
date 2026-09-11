# SRT Terrain-Enhanced Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional multi-TPKG terrain elevation, the validated no-XML joint COLMAP/SRT/DJI pose pipeline, six-degree-of-freedom residual fitting, and selectable-resolution calibrated rendering to the existing `srt_fixed_track_visual_pose` platform workflow.

**Architecture:** Keep one workflow identity and add terrain as a project-level immutable asset collection. Promote the successful experimental pose, terrain, and renderer code into focused production modules, pass their versioned artifacts through the existing trajectory/workbench/render job boundaries, and preserve the relative-height fallback when terrain is missing or incomplete.

**Tech Stack:** Python 3.11, NumPy, SciPy, OpenCV, SQLite/GeoPackage, FFmpeg/ffprobe, COLMAP CLI, vanilla JavaScript, pytest.

## Global Constraints

- Keep the workflow identifier `srt_fixed_track_visual_pose`; do not add a terrain-specific workflow.
- TPKG is optional and repeatable; upload order must not affect merged results.
- Do not use Bentley XML as an input or dependency.
- Terrain never participates in COLMAP feature matching or bundle adjustment.
- Preserve all existing dirty worktree edits and stage only files belonging to each task.
- Use integer user FOV as the RADIAL self-calibration initial value.
- Effective terrain requires at least 95% SRT route coverage within 160 metres of a control.
- The current platform has no trusted home/takeoff coordinate input, so every new terrain-backed task is published as partial-height and requires at least two manual keyframes; never infer takeoff from an arbitrary near-zero `rel_alt` sample.
- Render choices are 720p, 1080p, source, and conditional 4K; output is H.264 at 30 FPS.
- Changing only render resolution must not rerun pose, terrain, or route fitting.

---

### Task 1: Promote the TPKG terrain domain

**Files:**
- Create: `cadscene/terrain/__init__.py`
- Create: `cadscene/terrain/tpkg.py`
- Create: `cadscene/terrain/drape.py`
- Test: `tests/terrain/test_tpkg.py`
- Test: `tests/terrain/test_drape.py`

**Interfaces:**
- Produces: `inspect_tpkg(path: Path) -> TerrainSourceSummary`
- Produces: `load_tpkg(path: Path, project_lon_lat: Callable[[float, float], tuple[float, float]]) -> TerrainControlSet`
- Produces: `merge_terrain_controls(items: Sequence[TerrainControlSet]) -> TerrainControlSet`
- Produces: `route_coverage(route_xy: np.ndarray, controls: TerrainControlSet, max_distance_m: float = 160.0) -> TerrainCoverage`
- Produces: `drape_cad_segments(starts_xy, ends_xy, colors_bgr, controls, max_distance_m=160.0) -> DrapedCadSegments`

- [ ] **Step 1: Write failing GeoPackage parser and merge tests**

Create minimal SQLite fixtures with `PointZ` and `LineStringZ` blobs and assert preserved XYZ, deterministic source fingerprints, source summaries, and order-independent merged arrays.

```python
def test_merge_is_independent_of_upload_order(tmp_path: Path) -> None:
    first = load_tpkg(_tpkg(tmp_path / "a.tpkg", z=120.0), project_lon_lat=_identity)
    second = load_tpkg(_tpkg(tmp_path / "b.tpkg", z=130.0), project_lon_lat=_identity)
    assert merge_terrain_controls((first, second)).fingerprint == merge_terrain_controls((second, first)).fingerprint
```

- [ ] **Step 2: Run parser tests and verify failure**

Run: `pytest tests/terrain/test_tpkg.py -q`

Expected: FAIL because `cadscene.terrain.tpkg` does not exist.

- [ ] **Step 3: Implement the supported GeoPackage decoder and immutable domain types**

Implement explicit little/big-endian GeoPackage headers, `PointZ`, `LineStringZ`, and `PolygonZ`, finite coordinate validation, style/name preservation, and read-only SQLite access. Sort source collections by SHA-256 before concatenation.

```python
@dataclass(frozen=True)
class TerrainControlSet:
    points_xyz: np.ndarray
    segment_starts_xyz: np.ndarray
    segment_ends_xyz: np.ndarray
    point_source_ids: tuple[str, ...]
    segment_source_ids: tuple[str, ...]
    fingerprint: str
```

- [ ] **Step 4: Write failing coverage, conflict, and draping tests**

Assert 95% mode boundaries, 2 m control-segment sampling, the 160 m cutoff, nearest-control selection, source provenance, and preservation of original CAD colours.

```python
def test_drape_uses_nearest_control_and_preserves_colour() -> None:
    result = drape_cad_segments(starts, ends, colors, controls, max_distance_m=160.0)
    assert result.starts_xyz[0, 2] == pytest.approx(132.5)
    assert result.colors_bgr.tolist() == colors.tolist()
    assert result.source_ids == (controls.segment_source_ids[0],)
```

- [ ] **Step 5: Implement spatial indexing, coverage, conflicts, and draping**

Use `scipy.spatial.cKDTree`. Sample line/polygon control segments every 2 m, include `PointZ` directly, select the nearest candidate within 160 m, and report source pairs whose controls are within 2 m horizontally but differ by more than 3 m in Z.

- [ ] **Step 6: Run terrain tests**

Run: `pytest tests/terrain/test_tpkg.py tests/terrain/test_drape.py -q`

Expected: PASS.

- [ ] **Step 7: Commit terrain domain**

```bash
git add cadscene/terrain tests/terrain
git commit -m "feat: add deterministic TPKG terrain controls"
```

### Task 2: Promote DJI telemetry, adaptive sampling, and joint pose alignment

**Files:**
- Create: `cadscene/dji/__init__.py`
- Create: `cadscene/dji/metadata.py`
- Create: `cadscene/sfm/adaptive_sampling.py`
- Create: `cadscene/srt/joint_pose_alignment.py`
- Modify: `cadscene/sfm/reconstruction.py`
- Modify: `cadscene/cli/run_sfm.py`
- Test: `tests/dji/test_metadata.py`
- Test: `tests/sfm/test_adaptive_sampling.py`
- Test: `tests/srt/test_joint_pose_alignment.py`
- Test: `tests/sfm/test_reconstruction.py`

**Interfaces:**
- Produces: `load_dji_pose_priors(video: Path, source_ordinals: Sequence[int]) -> DjiPosePriorTrack`
- Produces: `select_adaptive_ordinals(positions, rotations, sharpness, ...) -> AdaptiveFramePlan`
- Produces: `solve_joint_alignment(...) -> JointAlignmentResult`
- Extends: `ReconstructionConfig.explicit_source_frames: tuple[int, ...] | None`

- [ ] **Step 1: Port experimental tests as failing production tests**

Cover protobuf wire decoding, Bentley X-right/Y-down composition, global shared Sim3 recovery, robust position outlier handling, one-second base sampling, half-second retention on turns or fast motion, and sharpness rescue.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/dji/test_metadata.py tests/sfm/test_adaptive_sampling.py tests/srt/test_joint_pose_alignment.py -q`

Expected: FAIL because production modules do not exist.

- [ ] **Step 3: Implement guarded DJI metadata decoding**

Use ffprobe to discover the `djmd` data stream rather than assuming stream index 1. Validate packet count and protobuf paths; return an unavailable result with a diagnostic reason for unsupported DJI layouts instead of inventing attitude values.

```python
@dataclass(frozen=True)
class DjiPosePriorTrack:
    source_ordinals: np.ndarray
    world_from_camera: np.ndarray
    packet_count: int
    convention: str
```

- [ ] **Step 4: Implement adaptive sampling and joint alignment**

Promote the validated thresholds: one-second base, half-second candidates, 18 m translation, 0.75 degree attitude change, 5 degree route turn, 10th-percentile blur detection, and 1.5 sharpness rescue ratio. Promote the robust Umeyama initialisation and `soft_l1` least-squares solver with 20-second XYZ knots.

- [ ] **Step 5: Add explicit source-frame extraction to reconstruction**

When `explicit_source_frames` is present, extract exactly those source ordinals and preserve their names as `frame_<source ordinal>.jpg`; otherwise retain the current `start_frame/frame_step` behaviour.

- [ ] **Step 6: Switch fixed-track COLMAP to RADIAL self-calibration**

Generate camera parameters `(focal, cx, cy, 0, 0)`, allow focal and radial refinement, use 8000 features, sequential overlap 10, 15 global BA iterations, and deterministic seed 0 where the installed COLMAP supports it. Unsupported optional seed flags must not break older COLMAP builds.

- [ ] **Step 7: Run pose and reconstruction tests**

Run: `pytest tests/dji tests/sfm/test_adaptive_sampling.py tests/srt/test_joint_pose_alignment.py tests/sfm/test_reconstruction.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the production pose solver**

```bash
git add cadscene/dji cadscene/sfm cadscene/srt/joint_pose_alignment.py cadscene/cli/run_sfm.py tests/dji tests/sfm tests/srt/test_joint_pose_alignment.py
git commit -m "feat: add adaptive joint SRT visual pose solver"
```

### Task 3: Publish calibrated fixed-track trajectory artifacts

**Files:**
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Modify: `cadscene/srt/fixed_track_visual_pose.py`
- Test: `tests/projects/test_workflow_adapters.py`
- Test: `tests/srt/test_fixed_track_visual_pose.py`
- Test: `tests/cli/test_build_srt_fixed_track_visual_pose.py`

**Interfaces:**
- Produces: `02_srt_visual_pose/camera_calibration.json`
- Produces: `02_srt_visual_pose/joint_alignment.json`
- Preserves: `02_srt_visual_pose/camera_trajectory_visual_pose.json`
- Preserves: `03_alignment/camera_track_pred.json`
- Preserves: `05_viewer_scene/sfm_viewer_scene.json`

- [ ] **Step 1: Write failing adapter command tests**

Assert RADIAL camera configuration, focal/radial refinement, adaptive frame plan input, and the calibrated artifact paths.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `pytest tests/projects/test_workflow_adapters.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose.py -q`

Expected: FAIL on the current PINHOLE/no-calibration contract.

- [ ] **Step 3: Build SRT and DJI priors before COLMAP extraction**

Write `adaptive_frame_plan.json` into the immutable attempt. Feed its selected source frames to `run_sfm`; if DJI metadata is unavailable, keep the SRT-position/COLMAP path and publish a warning rather than falling back to the old OpenCV attitude estimator.

- [ ] **Step 4: Use the joint solver and publish source-resolution calibration**

Scale `focal_px`, `cx_px`, and `cy_px` from solve resolution to source resolution; preserve `k1` and `k2`. Publish per-frame world-from-camera quaternions and centers in CAD-local metres.

- [ ] **Step 5: Preserve the workbench schema and expose diagnostics**

Populate viewer scene metadata with `reconstruction_resolution`, actual solve size, source size, calibrated FOV, model, distortion, joint residual statistics, and DJI prior availability.

- [ ] **Step 6: Run fixed-track tests**

Run: `pytest tests/projects/test_workflow_adapters.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose.py -q`

Expected: PASS.

- [ ] **Step 7: Commit calibrated trajectory publication**

```bash
git add cadscene/projects/workflow_adapters.py cadscene/cli/build_srt_fixed_track_visual_pose.py cadscene/srt/fixed_track_visual_pose.py tests/projects/test_workflow_adapters.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose.py
git commit -m "feat: publish calibrated fixed-track trajectories"
```

### Task 4: Add immutable multi-TPKG project uploads

**Files:**
- Modify: `cadscene/projects/uploads.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/serializers.py`
- Test: `tests/projects/test_uploads.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`

**Interfaces:**
- Adds: `POST /api/projects/{project}/uploads/terrain`
- Adds: `DELETE /api/projects/{project}/terrain-sources/{fingerprint}`
- Adds: `ProjectService.register_uploaded_terrain_source(...)`
- Adds: `ProjectService.delete_terrain_source(...)`
- Stores: `source_assets["terrain_sources"]` as a list of immutable descriptors

- [ ] **Step 1: Write failing upload collection tests**

Assert two `.tpkg` files coexist, duplicate content is idempotent, unsupported extensions fail, deleting one source preserves the other, and project snapshots return the list.

- [ ] **Step 2: Run upload/API tests and verify failure**

Run: `pytest tests/projects/test_uploads.py tests/cli/test_serve_viewer_project_api.py -q`

Expected: FAIL because terrain is not a supported upload type.

- [ ] **Step 3: Extend validated uploads for content-addressed terrain files**

Add `.tpkg` validation using `inspect_tpkg`. Keep each immutable `terrain-<sha256>.tpkg` and its immutable validation report; do not use the single canonical terrain report as project authority.

- [ ] **Step 4: Add atomic collection registration and deletion**

Sort descriptors by fingerprint. Every mutation increments the project revision and clears only `_terrain_analysis`, `_terrain_cad`, and render freshness markers; it must not clear trajectory or workbench output references.

- [ ] **Step 5: Add routes and optimistic revision checks**

Use the existing `expectedRevision/useCurrentRevision` upload convention and require `expected_revision` on deletion.

- [ ] **Step 6: Run upload/API tests**

Run: `pytest tests/projects/test_uploads.py tests/cli/test_serve_viewer_project_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit multi-file terrain assets**

```bash
git add cadscene/projects/uploads.py cadscene/projects/http_api.py cadscene/projects/service.py cadscene/projects/serializers.py tests/projects/test_uploads.py tests/cli/test_serve_viewer_project_api.py
git commit -m "feat: add multi-file terrain project assets"
```

### Task 5: Build and expose versioned terrain context

**Files:**
- Create: `cadscene/terrain/context.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Test: `tests/terrain/test_context.py`
- Test: `tests/projects/test_fixed_track_visual_pose_configuration.py`

**Interfaces:**
- Produces: `02_srt_visual_pose/terrain_context.json`
- Produces: `02_srt_visual_pose/terrain_controls.npz`
- Adds trajectory parameters: `terrain_source_paths`, `terrain_source_fingerprints`
- Adds viewer metadata: `terrain_mode`, `terrain_coverage`, `terrain_height_range_m`, `terrain_warnings`

- [ ] **Step 1: Write failing context tests**

Assert `partial` whenever valid controls exist but no trusted takeoff datum is available (including coverage `>= 0.95`), `relative` with no valid controls, and binding to CAD/georeference/source fingerprints.

- [ ] **Step 2: Run context tests and verify failure**

Run: `pytest tests/terrain/test_context.py tests/projects/test_fixed_track_visual_pose_configuration.py -q`

Expected: FAIL because terrain context does not exist.

- [ ] **Step 3: Implement context compilation**

Project WGS84 controls through the confirmed CAD georeference, calculate coverage against the full SRT route, serialize compressed numeric controls, and publish a human-readable JSON report.

- [ ] **Step 4: Integrate context into trajectory preparation and viewer scene**

Pass immutable terrain paths/fingerprints in the trajectory job identity. A terrain failure publishes `terrain_mode=relative` plus warnings and must not fail COLMAP.

- [ ] **Step 5: Run context and workflow tests**

Run: `pytest tests/terrain/test_context.py tests/projects/test_fixed_track_visual_pose_configuration.py tests/srt/test_fixed_track_visual_pose.py -q`

Expected: PASS.

- [ ] **Step 6: Commit terrain context**

```bash
git add cadscene/terrain/context.py cadscene/projects/service.py cadscene/projects/workflow_adapters.py cadscene/cli/build_srt_fixed_track_visual_pose.py tests/terrain/test_context.py tests/projects/test_fixed_track_visual_pose_configuration.py tests/srt/test_fixed_track_visual_pose.py
git commit -m "feat: bind terrain context to fixed-track trajectories"
```

### Task 6: Promote the calibrated 3D renderer

**Files:**
- Create: `cadscene/rendering/calibration.py`
- Create: `cadscene/rendering/calibrated_overlay.py`
- Create: `cadscene/cad/text_annotations.py`
- Modify: `cadscene/cli/render_overlay.py`
- Modify: `cadscene/projects/workbench_render_adapter.py`
- Test: `tests/rendering/test_calibration.py`
- Test: `tests/rendering/test_calibrated_overlay.py`
- Test: `tests/projects/test_workbench_render_adapter.py`

**Interfaces:**
- Produces: `CalibratedCameraModel.scaled_to(width: int, height: int)`
- Produces: `render_calibrated_overlay_video(config: CalibratedRenderConfig, progress_callback=None) -> RenderOverlayResult`
- Adds CLI flags: `--camera-calibration`, repeatable `--terrain-source`, `--terrain-context`, `--output-resolution`

- [ ] **Step 1: Port experiment renderer tests as failing production tests**

Cover per-vertex Z projection, RADIAL distortion, source-resolution calibration scaling, original CAD colours, UTF-8 DXF text recovery, Chinese TrueType rendering, and exact source-frame lookup.

- [ ] **Step 2: Run renderer tests and verify failure**

Run: `pytest tests/rendering/test_calibration.py tests/rendering/test_calibrated_overlay.py -q`

Expected: FAIL because calibrated production renderer modules do not exist.

- [ ] **Step 3: Implement calibrated projection and CAD three-dimensionalisation**

Promote only reusable functions from the experiment. Keep the existing renderer intact and dispatch to the calibrated renderer only when `--camera-calibration` is provided for `srt_fixed_track_visual_pose`.

- [ ] **Step 4: Implement resolution presets**

Normalize `720p`, `1080p`, and `source`; allow `4k` only when source dimensions are at least 3840x2160. Preserve aspect ratio and scale focal/principal point consistently. Keep `k1/k2` unchanged in normalized radial coordinates.

- [ ] **Step 5: Implement FFmpeg H.264/30 FPS output**

Use raw BGR input to FFmpeg, a browser-compatible H.264 encoder available on the host, `yuv420p`, and `+faststart`. Record final dimensions, frame count, 30 FPS, calibration, terrain provenance, and render timings.

- [ ] **Step 6: Pass calibrated artifacts through the render adapter**

Resolve calibration and terrain context beside the current trajectory output. Add all inputs and output resolution to the immutable render command and validator contract.

- [ ] **Step 7: Run renderer and adapter tests**

Run: `pytest tests/rendering/test_calibration.py tests/rendering/test_calibrated_overlay.py tests/projects/test_workbench_render_adapter.py -q`

Expected: PASS.

- [ ] **Step 8: Commit calibrated rendering**

```bash
git add cadscene/rendering cadscene/cad/text_annotations.py cadscene/cli/render_overlay.py cadscene/projects/workbench_render_adapter.py tests/rendering tests/projects/test_workbench_render_adapter.py
git commit -m "feat: add terrain-aware calibrated video rendering"
```

### Task 7: Add render resolution to immutable render jobs

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/render_adapters.py`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/style.css`
- Test: `tests/projects/test_render_jobs.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Adds request field: `output_resolution: "720p" | "1080p" | "source" | "4k"`
- Adds render parameter: `RenderInputs.parameters["output_resolution"]`
- Adds render identity dependency: `output_resolution`

- [ ] **Step 1: Write failing render identity and API tests**

Assert default `1080p`, same resolution is idempotent, a different resolution creates a new render job without changing trajectory dependencies, and non-4K source rejects `4k`.

- [ ] **Step 2: Run render job tests and verify failure**

Run: `pytest tests/projects/test_render_jobs.py tests/cli/test_serve_viewer_project_api.py -q`

Expected: FAIL because render jobs ignore output resolution.

- [ ] **Step 3: Add backend normalization and identity**

Validate against the project media spec, include output resolution in the job fingerprint and adapter parameters, and leave trajectory/workbench revisions unchanged.

- [ ] **Step 4: Add the render-stage selector**

Render `720p`, `1080p`, and `原始分辨率`; conditionally add `4K` when source dimensions qualify. Send the selected value in both preflight and enqueue requests and restore it while a job is active.

- [ ] **Step 5: Run backend and static UI tests**

Run: `pytest tests/projects/test_render_jobs.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_workflow_ui_static.py -q`

Expected: PASS.

- [ ] **Step 6: Commit render resolution jobs**

```bash
git add cadscene/projects/http_api.py cadscene/projects/service.py cadscene/projects/render_adapters.py apps/web_camera_viewer tests/projects/test_render_jobs.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_workflow_ui_static.py
git commit -m "feat: add selectable project render resolution"
```

### Task 8: Add terrain upload and status UI

**Files:**
- Modify: `apps/workflow_portal/index.html`
- Modify: `apps/workflow_portal/workflow_portal.js`
- Modify: `apps/workflow_portal/portal_api.js`
- Modify: `apps/workflow_portal/style.css`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/style.css`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Test: `tests/viewer/test_workflow_portal_static.py`
- Test: `tests/viewer/test_project_workspace_static.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: snapshot `assets.terrain_sources` and clip viewer metadata terrain fields
- Produces: repeatable terrain upload calls and workbench status cards

- [ ] **Step 1: Write failing static UI tests**

Assert a multiple `.tpkg` input, per-file status list, conditional analysis stage, terrain mode/coverage/warning fields, camera calibration fields, and visible trajectory/point-cloud/frustum controls.

- [ ] **Step 2: Run static tests and verify failure**

Run: `pytest tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py -q`

Expected: FAIL because terrain UI is absent.

- [ ] **Step 3: Implement portal multi-upload**

Upload each selected file independently, keep successful files when another fails, allow removal, and wait for all selected terrain upload promises before starting analysis. Video and CAD remain the only required assets.

- [ ] **Step 4: Implement conditional progress and project status**

Show “解析高程数据” only when terrain sources exist. Display valid file count, mode, coverage percentage, Z range, and conflict/coverage warnings.

- [ ] **Step 5: Implement workbench controls and keyframe gate copy**

Keep all six-degree-of-freedom controls enabled. Current new tasks require at least two keyframes in all partial/relative terrain modes. Never accept exactly one manual keyframe; preserve historical validated absolute-datum behavior when reading old artifacts.

- [ ] **Step 6: Run static UI tests**

Run: `pytest tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py -q`

Expected: PASS.

- [ ] **Step 7: Commit terrain UI**

```bash
git add apps/workflow_portal apps/project_workspace apps/web_camera_viewer tests/viewer
git commit -m "feat: expose terrain-enhanced SRT workflow UI"
```

### Task 9: End-to-end regression and local acceptance

**Files:**
- Modify: `docs/current/project-workflow-routing.md`
- Modify: `tests/docs/test_current_documentation.py`
- Create: `tests/integration/test_srt_terrain_workflow.py`

**Interfaces:**
- Verifies all prior task contracts together.

- [ ] **Step 1: Add a synthetic end-to-end test**

Create minimal video/frame-map, SRT, CAD, sparse trajectory, RADIAL calibration, and two TPKG fixtures. Assert project upload, effective terrain context, workbench metadata, zero-keyframe confirmation, 720p render request, and packaged output proof.

- [ ] **Step 2: Run the focused feature suite**

Run: `pytest tests/terrain tests/dji tests/sfm/test_adaptive_sampling.py tests/srt/test_joint_pose_alignment.py tests/srt/test_fixed_track_visual_pose.py tests/projects/test_fixed_track_visual_pose_configuration.py tests/projects/test_render_jobs.py tests/projects/test_workbench_render_adapter.py tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py tests/integration/test_srt_terrain_workflow.py -q`

Expected: PASS.

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q`

Expected: PASS with no new failures.

- [ ] **Step 4: Run the real local smoke test**

Start the project service, create a fresh project using the current MP4, DXF, incomplete SRT, `zhix.tpkg`, and `station.tpkg`, confirm the 118°50′ CAD georeference, enter the correct workbench, inspect live trajectory/point cloud/frusta, and render a 1080p/30 FPS output without XML.

- [ ] **Step 5: Verify output media and provenance**

Run `ffprobe` on the rendered video and assert H.264, 1920x1080, 30 FPS, expected source interval, non-zero duration, and a render report listing both terrain fingerprints, RADIAL calibration, and the selected resolution.

- [ ] **Step 6: Update routing documentation**

Document the unified workflow, three terrain modes, keyframe gates, renderer dispatch, output resolution choices, and invalidation rules.

- [ ] **Step 7: Commit integration coverage and docs**

```bash
git add tests/integration/test_srt_terrain_workflow.py docs/current/project-workflow-routing.md tests/docs/test_current_documentation.py
git commit -m "test: cover terrain-enhanced SRT workflow"
```

## Plan Self-Review

- Spec coverage: Tasks 1 and 5 cover parsing, merging, projection, coverage, conflicts, and fallback; Tasks 2 and 3 cover the validated no-XML pose route; Tasks 4 and 8 cover project assets and UI; Tasks 6 and 7 cover calibrated rendering and resolution; Task 9 covers end-to-end acceptance and documentation.
- Placeholder scan: The plan contains no unresolved implementation placeholder; every task names files, interfaces, failing tests, implementation actions, verification commands, and a scoped commit.
- Type consistency: `TerrainControlSet`, `TerrainCoverage`, `DrapedCadSegments`, `DjiPosePriorTrack`, `JointAlignmentResult`, calibrated artifacts, `output_resolution`, and terrain metadata names are consistent across producer and consumer tasks.
