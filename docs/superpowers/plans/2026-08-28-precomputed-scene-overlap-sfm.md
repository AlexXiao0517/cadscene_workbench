# Precomputed Scene Overlap SfM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run SfM once per same-scene clip over a precomputed four-second-per-side solve interval, then bridge adjacent routes without invoking SfM at click time.

**Architecture:** Preserve the current non-overlapping core video/frame map for workbench, render and concat. Add one immutable solve-video export for multi-segment `sfm_only` clips, partition the single solve reconstruction into full solve and core-reindexed trajectories, and change the scene-bridge worker to consume those artifacts instead of exporting media and running temporary SfM.

**Tech Stack:** Python 3.11, exact `Fraction`/integer PTS, existing ProjectService queue and JSON repositories, FFmpeg clip exporter, pycolmap SfM adapter, existing alignment pipeline, vanilla JavaScript workspace, pytest.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench\.worktrees\async-job-progress-scene-positioning` on `codex/async-job-progress-scene-positioning`; do not modify or merge main.
- Each adjacent boundary expands both participating clips by exactly 4 seconds, snapped to decoded-frame PTS.
- Solve intervals never cross `scene_index`; single-segment scenes have no bridge capability.
- Core source PTS, rendered frame count, render frame map, output ordinal and Stage 8 concat partition must not change.
- Only same-scene adjacent `sfm_only` clips participate; Pure Rotation, SRT and cross-scene inheritance stay unchanged.
- A trajectory attempt contains exactly one `cadscene.cli.run_sfm` command.
- Clicking a bridge action must contain no video export and no `cadscene.cli.run_sfm` command.
- Old adapter results are not used as a slow fallback; users rerun trajectory solve in this isolated branch.
- Route fitting must stop at route refinement and must not auto-run quality detection.

---

### Task 1: Exact same-scene solve-window contract

**Files:**
- Modify: `cadscene/projects/scene_bridges.py`
- Modify: `tests/projects/test_scene_bridges.py`

**Interfaces:**
- Produces: `derive_scene_solve_interval(clips: Sequence[ClipDefinition], clip_id: str, source_frame_index: DecodedFrameIndex, overlap_seconds: Fraction = Fraction(4, 1)) -> SolveInterval`.
- Produces: `scene_bridge_neighbor(clips: Sequence[ClipDefinition], clip_id: str, direction: str) -> ClipDefinition | None`.
- `SolveInterval` continues to expose exact `start_pts`, `end_pts_exclusive`, `core_start_pts`, `core_end_pts_exclusive` and core indices.

- [ ] **Step 1: Write failing scene-boundary tests**

```python
def test_middle_clip_expands_four_seconds_each_side_inside_its_scene():
    interval = derive_scene_solve_interval(
        clips=_clips(scene_segments=(3, 1)),
        clip_id="scene-1-clip-2",
        source_frame_index=_frame_index(fps=25),
    )
    assert interval.start_pts == _pts("00:52")
    assert interval.end_pts_exclusive == _pts("02:08")

def test_scene_edge_never_uses_frames_from_the_next_scene():
    interval = derive_scene_solve_interval(
        clips=_clips(scene_segments=(2, 2)),
        clip_id="scene-1-clip-2",
        source_frame_index=_frame_index(fps=25),
    )
    assert interval.end_pts_exclusive == _scene_end_pts(1)

def test_single_segment_scene_has_core_only_and_no_neighbors():
    clips = _clips(scene_segments=(1, 2))
    interval = derive_scene_solve_interval(clips, "scene-1-clip-1", _frame_index())
    assert interval.start_pts == interval.core_start_pts
    assert interval.end_pts_exclusive == interval.core_end_pts_exclusive
    assert scene_bridge_neighbor(clips, "scene-1-clip-1", "down") is None
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py -q`

Expected: failures report that `derive_scene_solve_interval` and `scene_bridge_neighbor` do not exist.

- [ ] **Step 3: Implement scene-local expansion using exact frames**

```python
same_scene = sorted(
    (item for item in clips if _scene_index(item) == _scene_index(current)),
    key=lambda item: (_segment_index(item), _render_order(item), item.clip_id),
)
scene_start = int(same_scene[0].analysis["source_start_pts"])
scene_end = int(same_scene[-1].analysis["source_end_pts_exclusive"])
requested_start = max(scene_start, Fraction(core_start) - overlap_seconds / time_base)
requested_end = min(scene_end, Fraction(core_end) + overlap_seconds / time_base)
selected = tuple(frame for frame in source_frame_index.frames if requested_start <= frame.pts < requested_end)
```

Expansion is enabled only when `len(same_scene) > 1`; otherwise return the exact core interval. Reject duplicate/non-monotonic segment indices and mixed time bases.

- [ ] **Step 4: Add VFR outward-snap and cross-scene rejection tests**

```python
def test_vfr_expansion_uses_real_decoded_pts_without_fps_rounding():
    interval = derive_scene_solve_interval(_vfr_clips(), "clip-b", _vfr_index())
    assert interval.start_pts == _vfr_pts_before_requested_start()
    assert interval.end_pts_exclusive == _vfr_exclusive_end_after_requested_end()

def test_neighbor_rejects_unknown_direction():
    with pytest.raises(ValueError, match="direction must be up or down"):
        scene_bridge_neighbor(_clips(scene_segments=(2,)), "clip-1", "left")
```

- [ ] **Step 5: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py -q`

Expected: PASS.

Commit: `git add cadscene/projects/scene_bridges.py tests/projects/test_scene_bridges.py && git commit -m "feat: derive same-scene sfm solve windows"`

### Task 2: Immutable solve-video export and queue dependency

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/video_analysis/test_clip_export.py`

**Interfaces:**
- Produces job type `sfm_solve_export`, adapter `sfm_solve_export`, version `1`.
- Published outputs: `solve_video:<clip_id>` and `solve_frame_map:<clip_id>`.
- Produces `_sfm_solve_physical_inputs(clip: ClipDefinition, jobs: Sequence[QueueJob]) -> tuple[Path, Path]`.
- Existing `_render_physical_inputs` remains core-only.

- [ ] **Step 1: Write failing queue and interval tests**

```python
def test_multi_segment_sfm_trajectory_depends_on_one_solve_export(api):
    result = enqueue_trajectory(api, clip_ids=("clip-2",))
    trajectory = api.service.queue.get(result.job_ids[0])
    dependencies = [api.service.queue.get(job_id) for job_id in trajectory.depends_on_job_ids]
    assert [job.job_type for job in dependencies].count("sfm_solve_export") == 1

def test_solve_export_manifest_is_scene_clamped(service):
    job = queued_solve_export(service, "last-clip-of-scene-1")
    plan = service.prepare_job(job.project_id, job.job_id)
    manifest = json.loads((Path(job.attempts[-1].directory) / "solve_export_manifest.json").read_text())
    assert manifest["clips"][0]["source_end_pts_exclusive"] == scene_1_end_pts()

def test_single_segment_and_pure_rotation_do_not_queue_solve_export(api):
    assert solve_export_jobs(enqueue_single_sfm_scene(api)) == []
    assert solve_export_jobs(enqueue_pure_rotation(api)) == []
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k "solve_export or trajectory_depends" -q`

Expected: no `sfm_solve_export` dependency exists.

- [ ] **Step 3: Implement solve-export identity and execution plan**

```python
identity = {
    "job_type": "sfm_solve_export",
    "adapter_version": "1",
    "clip_id": clip.clip_id,
    "analysis_revision": clip.analysis_revision,
    "core_interval": _authoritative_interval(clip),
    "solve_interval": solve_interval.to_dict(),
    "source_assets": _clip_input_identity(clip, project.source_assets),
    "algorithm_version": "same-scene-overlap-4s-v1",
}
```

The execution plan writes a one-clip manifest from `solve_interval`, calls the existing exporter with `--allow-subset`, validates the exact map interval and publishes only solve-prefixed outputs. Reuse an existing current solve-export job idempotently.

- [ ] **Step 4: Change trajectory enqueue dependency selection**

For multi-segment `sfm_only`, always attach the current/new solve-export job. Attach the existing core clip-export dependency independently when the core physical video is unavailable. Do not alter render input lookup.

- [ ] **Step 5: Add core-map invariance test**

```python
def test_solve_export_does_not_replace_core_render_inputs(project_with_solve_export):
    before = core_frame_map_bytes(project_with_solve_export)
    finish_solve_export(project_with_solve_export)
    assert core_frame_map_bytes(project_with_solve_export) == before
    assert render_physical_video(project_with_solve_export).name == core_video_name()
```

- [ ] **Step 6: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py tests/video_analysis/test_clip_export.py -k "solve_export or core_render" -q`

Expected: PASS.

Commit: `git add cadscene/projects/service.py cadscene/projects/http_api.py tests/projects/test_workbench_sessions.py tests/video_analysis/test_clip_export.py && git commit -m "feat: pre-export same-scene sfm solve media"`

### Task 3: One SfM publishes solve and core trajectories

**Files:**
- Create: `cadscene/sfm/trajectory_partition.py`
- Create: `cadscene/cli/partition_sfm_trajectory.py`
- Create: `tests/sfm/test_trajectory_partition.py`
- Modify: `cadscene/projects/adapters.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/projects/service.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Modify: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- Produces `partition_sfm_trajectory(raw_path: Path, solve_frame_map_path: Path, core_frame_map_path: Path, *, solve_output_path: Path, core_output_path: Path) -> TrajectoryPartitionResult`.
- `AdapterInputs` adds `core_frame_map_path: Path | None = None`.
- `sfm_only` adapter version becomes `2`.
- Trajectory outputs add `solve_trajectory`, `solve_frame_map` and `core_frame_map`; existing `trajectory` remains the core-reindexed path.

- [ ] **Step 1: Write failing exact PTS partition tests**

```python
def test_partition_binds_solve_poses_to_source_pts_and_reindexes_core(tmp_path):
    result = partition_sfm_trajectory(
        _raw_trajectory(tmp_path, pose_frames=(0, 5, 10, 15)),
        _solve_map(tmp_path, source_pts=(80, 90, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230)),
        _core_map(tmp_path, source_pts=(120, 130, 140, 150, 160, 170, 180, 190, 200)),
        solve_output_path=tmp_path / "camera_trajectory_solve.json",
        core_output_path=tmp_path / "camera_trajectory.json",
    )
    solve = json.loads(result.solve_path.read_text())
    core = json.loads(result.core_path.read_text())
    assert [pose["source_pts"] for pose in solve["poses"]] == [80, 130, 180, 230]
    assert [(pose["frame_index"], pose["source_pts"]) for pose in core["poses"]] == [(1, 130), (6, 180)]

def test_partition_rejects_pose_frame_outside_solve_map(tmp_path):
    with pytest.raises(ValueError, match="outside solve frame map"):
        partition_sfm_trajectory(
            _raw_trajectory(tmp_path, pose_frames=(99,)),
            _solve_map(tmp_path, source_pts=(80, 90, 100)),
            _core_map(tmp_path, source_pts=(90, 100)),
            solve_output_path=tmp_path / "camera_trajectory_solve.json",
            core_output_path=tmp_path / "camera_trajectory.json",
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/sfm/test_trajectory_partition.py -q`

Expected: collection fails because the partition module is missing.

- [ ] **Step 3: Implement atomic trajectory partitioning**

Load both exact maps, build `solve ordinal -> source PTS` and `source PTS -> core ordinal`, copy every solve pose with integer `source_pts`, and include only exact core PTS poses in the core trajectory with the core ordinal. Preserve intrinsics, dimensions, FPS and quality fields. Require at least two registered core poses.

- [ ] **Step 4: Write failing one-SfM adapter test**

```python
def test_sfm_adapter_runs_one_sfm_then_partitions_trajectory(inputs):
    commands = sfm_adapter_v2().build_commands(inputs)
    assert sum("cadscene.cli.run_sfm" in command for command in commands) == 1
    assert sum("cadscene.cli.partition_sfm_trajectory" in command for command in commands) == 1
```

- [ ] **Step 5: Wire adapter v2 and combined validation**

The SfM command consumes solve video. The partition command consumes solve/core maps and writes `02_sfm/camera_trajectory_solve.json` plus the compatible `02_sfm/camera_trajectory.json`. Validation hashes both trajectories and verifies every core pose source PTS exists in both maps. Single-segment scenes pass the same map as solve and core and still execute only one SfM.

- [ ] **Step 6: Verify old v1 trajectory is not current**

```python
def test_sfm_v1_job_requires_rerun_after_overlap_contract_upgrade(service):
    old = successful_sfm_job(service, adapter_version="1")
    assert service._current_input_fingerprint(old) != old.input_fingerprint
```

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/sfm/test_trajectory_partition.py tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/packaging/test_wheel_contents.py -k "partition or one_sfm or overlap_contract" -q`

Expected: PASS.

Commit: `git add cadscene/sfm/trajectory_partition.py cadscene/cli/partition_sfm_trajectory.py cadscene/projects/adapters.py cadscene/projects/workflow_adapters.py cadscene/projects/service.py tests/sfm/test_trajectory_partition.py tests/projects/test_workflow_adapters.py tests/projects/test_workbench_sessions.py tests/packaging/test_wheel_contents.py && git commit -m "feat: publish core and solve paths from one sfm"`

### Task 4: Remove SfM from the scene-bridge click path

**Files:**
- Modify: `cadscene/projects/scene_bridge_runner.py`
- Modify: `cadscene/projects/scene_bridges.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `tests/projects/test_scene_bridge_runner.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Scene-bridge algorithm version becomes `scene-overlap-precomputed-v2`.
- `SceneBridgeInputs` consumes source/target core video+map+trajectory and solve video+map+trajectory from current trajectory jobs.
- Produces `remap_core_track_to_solve(track: Mapping[str, object], core_map: FrameMap, solve_map: FrameMap) -> dict[str, object]`.
- Bridge runner phase list becomes `source_alignment`, `overlap_anchors`, `solve_alignment`, `core_alignment`.

- [ ] **Step 1: Replace the runner command contract with a failing test**

```python
def test_precomputed_bridge_never_exports_video_or_runs_sfm(tmp_path, recording_runner):
    run_scene_bridge(_precomputed_inputs(tmp_path), command_runner=recording_runner)
    flat = [token for command in recording_runner.commands for token in command]
    assert "cadscene.cli.run_sfm" not in flat
    assert "cadscene.cli.export_video_clips" not in flat
    assert recording_runner.stage_names == [
        "source_alignment", "solve_alignment", "core_alignment"
    ]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridge_runner.py -k precomputed -q`

Expected: current runner records `solve_export` and `target_sfm`.

- [ ] **Step 3: Implement core-track to solve-track mapping**

```python
for keyframe in confirmed_keyframes(dict(track)):
    core_frame = int(keyframe["frame"])
    source_pts = core_pts_by_ordinal[core_frame]
    solve_frame = solve_ordinal_by_pts[source_pts]
    remapped.append({**keyframe, "frame": solve_frame, "source_pts": source_pts})
```

Reject missing/non-integer PTS and non-monotonic mappings. Run source alignment against source solve trajectory/video. Use its CAD camera path and the two solve maps to select exact common PTS. Run target solve alignment, derive the target core seed, then run core alignment. Keep the existing immutable publication and stop-before-quality validation.

- [ ] **Step 4: Require current precomputed artifacts in service capability tests**

```python
def test_bridge_capability_requires_current_solve_artifacts(api):
    source = snapshot_clip(api, "clip-2")
    assert source["capabilities"]["can_bridge_down"] is False
    assert "重新轨迹反算" in source["capabilities"]["bridge_down_reason"]

def test_precomputed_solve_artifacts_enable_same_scene_adjacent_bridge(api):
    attach_sfm_v2_outputs(api, "clip-2")
    attach_sfm_v2_outputs(api, "clip-3")
    source = snapshot_clip(api, "clip-2")
    assert source["capabilities"]["can_bridge_down"] is True
```

- [ ] **Step 5: Add cross-scene, single-scene and chain-propagation tests**

```python
def test_bridge_rejects_adjacent_rows_from_different_scene_indices(api):
    response = enqueue_bridge(api, source="scene-1-last", target="scene-2-first")
    assert response.status == 400
    assert "同一场景" in response.body["error"]

def test_refined_target_can_bridge_the_next_same_scene_clip(api):
    publish_and_save_refined_bridge(api, source="clip-2", target="clip-3")
    assert snapshot_clip(api, "clip-3")["capabilities"]["can_bridge_down"] is True
```

- [ ] **Step 6: Run runner/service tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridge_runner.py tests/projects/test_workbench_sessions.py -k "scene_bridge or precomputed or chain" -q`

Expected: PASS.

Commit: `git add cadscene/projects/scene_bridge_runner.py cadscene/projects/scene_bridges.py cadscene/projects/service.py cadscene/projects/http_api.py cadscene/projects/workbench_sessions.py tests/projects/test_scene_bridge_runner.py tests/projects/test_workbench_sessions.py && git commit -m "feat: bridge adjacent routes without rerunning sfm"`

### Task 5: Status semantics, core invariants and real smoke

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `tests/projects/test_render_jobs.py`
- Modify: `tests/projects/test_concat.py`
- Modify: `tests/projects/test_concat_executor.py`

**Interfaces:**
- Snapshot primary row status remains the current trajectory/render status when an old bridge alone is `stale_input`.
- `scene_bridge.status` independently exposes `stale_input`, running progress and retry reason.
- No production render or concat interface changes.

- [ ] **Step 1: Write failing status test**

```python
def test_stale_bridge_does_not_mask_successful_trajectory(api):
    mark_bridge_stale(api, "clip-3")
    clip = snapshot_clip(api, "clip-3")
    assert clip["status"] == "success"
    assert clip["scene_bridge"]["status"] == "stale_input"
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k stale_bridge_does_not_mask -q`

Expected: primary clip status is `stale_input`.

- [ ] **Step 3: Implement independent scene-bridge status rendering**

Select scene-bridge as the row's active job only while it is queued/running/validating. For terminal stale bridge with a current trajectory, retain trajectory status and show “旧打通结果已失效，可重新打通” near the action/progress area.

- [ ] **Step 4: Add render and concat invariants**

```python
def test_precomputed_solve_map_never_changes_render_frame_map(project):
    core_before = authoritative_render_pairs(project)
    finish_solve_export_and_bridge(project)
    assert authoritative_render_pairs(project) == core_before

def test_concat_ignores_solve_overlap_frames(project):
    frame_map = build_project_concat(project)
    assert source_pts(frame_map) == expected_core_partition_pts(project)
    assert len(source_pts(frame_map)) == len(set(source_pts(frame_map)))
```

- [ ] **Step 5: Run focused and full verification**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py tests/sfm/test_trajectory_partition.py tests/projects/test_scene_bridge_runner.py tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/projects/test_concat_executor.py -q`

Expected: PASS.

Run: `python -m pytest -p no:cacheprovider`

Expected: all tests pass with only established skips.

Run: `python scripts/check_no_project_dependency.py`

Expected: success.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 6: Run isolated real `hygs` smoke**

Create a writable disposable copy of the `hygs` project manifests and outputs while referencing its video/CAD assets read-only. Rerun SfM v2 for `clip-0002`, `clip-0003` and `clip-0004`; verify each trajectory attempt contains one `run_sfm`, both adjacent pairs share at least two exact source PTS, clicking up/down contains no SfM command, target enters route refinement, and core frame-map hashes remain unchanged.

- [ ] **Step 7: Commit status and invariant coverage**

Commit: `git add cadscene/projects/http_api.py apps/project_workspace/project_workspace.js tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/projects/test_concat_executor.py && git commit -m "test: verify fast scene bridge preserves core frames"`

## Self-review

- Spec coverage: all scene-boundary, 4-second overlap, one-SfM, no-click-SfM, direct-upgrade, chain propagation, status and render/concat requirements map to a task.
- Placeholder scan: no TBD, TODO, deferred error handling or unspecified implementation step remains.
- Type consistency: solve export keys, `AdapterInputs.core_frame_map_path`, partition outputs, algorithm version and bridge phases are consistent across tasks.
