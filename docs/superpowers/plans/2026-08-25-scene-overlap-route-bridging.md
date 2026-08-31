# Scene Overlap Route Bridging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one saved SfM route automatically establish the CAD coordinate alignment of an adjacent same-scene clip through two exact shared source-PTS anchors, then open that clip at the existing route-refinement stage.

**Architecture:** Keep the current non-overlapping clip interval as the only render/concat authority. A new scene-bridge job exports a private four-second solve overlap, reuses the saved source manual track and existing alignment pipeline, fits the target solve reconstruction with two exact shared PTS poses, and derives two core-local target anchors. ProjectService publishes the validated candidate as an immutable bridge revision; workbench entry restores its seed and fitted alignment artifacts before creating the normal session.

**Tech Stack:** Python 3.11 dataclasses and JSON repositories, existing ProjectService queue/executor, FFmpeg clip exporter, pycolmap SfM adapter, existing `cadscene.cli.run_pipeline` alignment, vanilla JavaScript project workspace, pytest.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench\.worktrees\async-job-progress-scene-positioning` on `codex/async-job-progress-scene-positioning`; do not modify or merge main.
- Use source decoded-frame integer PTS plus exact time base; never use `currentTime * fps` or a fixed-FPS source-frame conversion.
- The existing core interval, rendered frame count, render frame map, output ordinal, and Stage 8 concat partition must not change.
- Only same-scene `sfm_only` clips participate; pure rotation and cross-scene inheritance stay unchanged.
- The target must have no active/pending/saved workbench result; failed or stale jobs must not publish an active bridge.
- Scene-bridge revisions are immutable and bind video, CAD, both clip revisions/frame maps, source workbench output, direction, overlap duration, and algorithm version.
- Route fitting must reuse the existing alignment pipeline and stop at route refinement; it must not auto-run quality detection.

---

### Task 1: Exact solve intervals and overlap anchors

**Files:**
- Create: `cadscene/projects/scene_bridges.py`
- Create: `tests/projects/test_scene_bridges.py`

**Interfaces:**
- Produces: `derive_solve_interval(core_start_pts: int, core_end_pts_exclusive: int, source_frames: Sequence[DecodedFrameTimestamp], time_base: Fraction, overlap_seconds: Fraction = Fraction(4, 1)) -> SolveInterval`.
- Produces: `select_overlap_anchors(direction: str, source_core_map: FrameMap, source_camera_path: Mapping[int, CameraPose], target_solve_map: FrameMap, target_registered_frames: Collection[int], time_base: Fraction, min_separation_seconds: Fraction = Fraction(1, 1)) -> tuple[SceneBridgeAnchor, SceneBridgeAnchor]`.
- Produces: `build_core_seed(core_map: FrameMap, core_registered_frames: Collection[int], solve_map: FrameMap, solve_camera_path: Mapping[int, CameraPose], fps: float) -> dict[str, object]`.

- [ ] **Step 1: Write failing interval tests**

```python
def test_solve_interval_expands_by_exact_pts_and_clamps_to_source_frames():
    frames = tuple(DecodedFrameTimestamp(i, i * 3600, 3600, "pts") for i in range(10))
    interval = derive_solve_interval(7200, 25200, frames, Fraction(1, 90000), Fraction(1, 25))
    assert (interval.start_pts, interval.end_pts_exclusive) == (3600, 28800)
    assert interval.core_start_index == 1

def test_solve_interval_at_source_start_does_not_invent_negative_pts():
    frames = tuple(DecodedFrameTimestamp(i, i * 3600, 3600, "pts") for i in range(4))
    interval = derive_solve_interval(0, 7200, frames, Fraction(1, 90000), Fraction(4, 1))
    assert interval.start_pts == 0
```

- [ ] **Step 2: Run interval tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py -q`
Expected: collection fails because `cadscene.projects.scene_bridges` does not exist.

- [ ] **Step 3: Implement immutable value types and exact frame selection**

```python
@dataclass(frozen=True)
class SolveInterval:
    start_pts: int
    end_pts_exclusive: int
    core_start_pts: int
    core_end_pts_exclusive: int
    core_start_index: int
    core_end_index_exclusive: int

def derive_solve_interval(...):
    requested = overlap_seconds / time_base
    first = min(frame.pts for frame in source_frames if frame.pts >= core_start_pts - requested)
    selected = tuple(frame for frame in source_frames if first <= frame.pts < core_end_pts_exclusive + requested)
    return SolveInterval(first, selected[-1].pts + (selected[-1].duration_pts or 1), ...)
```

- [ ] **Step 4: Write failing anchor/seed tests**

```python
def test_down_bridge_selects_two_exact_pts_from_source_tail_and_target_solve_head():
    anchors = select_overlap_anchors("down", source_map, source_path, target_map, {0, 25, 50}, Fraction(1, 25))
    assert [item.source_pts for item in anchors] == [100, 150]
    assert [item.target_frame for item in anchors] == [0, 50]

def test_overlap_with_only_one_registered_common_pts_is_rejected():
    with pytest.raises(SceneBridgeUnavailable, match="two common source PTS"):
        select_overlap_anchors("up", source_map, source_path, target_map, {7}, Fraction(1, 25))

def test_core_seed_uses_registered_core_endpoints_and_solve_cad_poses():
    seed = build_core_seed(core_map, {1, 8}, solve_map, solve_path, fps=25.0)
    assert [row["frame"] for row in seed["keyframes"]] == [1, 8]
    assert all(row["source"] == "scene_overlap_anchor" for row in seed["keyframes"])
```

- [ ] **Step 5: Run anchor tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py -q`
Expected: failures name the missing anchor-selection and seed functions.

- [ ] **Step 6: Implement exact PTS joins and core seed generation**

```python
common = sorted(set(source_pose_by_pts) & set(target_frame_by_pts))
eligible = [pts for pts in common if overlap_start <= pts < overlap_end]
if len(eligible) < 2:
    raise SceneBridgeUnavailable("scene overlap requires two common source PTS")
first = eligible[0]
last = next((pts for pts in reversed(eligible) if (pts - first) * time_base >= min_separation_seconds), None)
if last is None:
    raise SceneBridgeUnavailable("scene overlap anchors are too close")
```

- [ ] **Step 7: Run focused tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py -q`
Expected: PASS.

Commit: `git add cadscene/projects/scene_bridges.py tests/projects/test_scene_bridges.py && git commit -m "feat: derive exact scene overlap anchors"`

### Task 2: Deterministic bridge worker and candidate validation

**Files:**
- Create: `cadscene/projects/scene_bridge_runner.py`
- Create: `cadscene/cli/run_scene_bridge.py`
- Create: `tests/projects/test_scene_bridge_runner.py`
- Modify: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- Consumes: Task 1 interval, anchor and seed functions.
- Produces: `SceneBridgeInputs.from_json(path: Path)`, `run_scene_bridge(inputs: SceneBridgeInputs, *, command_runner=subprocess.run) -> Path`, and `validate_scene_bridge_candidate(candidate_root: Path, expected_identity: Mapping[str, object]) -> AdapterResult`.
- Candidate contains `scene_bridge_manifest.json`, `camera_track_seed.json`, `core_alignment/03_alignment`, and `core_alignment/05_viewer`.

- [ ] **Step 1: Write a failing runner contract test**

```python
def test_bridge_runner_uses_existing_alignment_for_source_solve_and_core(tmp_path, fake_commands):
    candidate = run_scene_bridge(_inputs(tmp_path), command_runner=fake_commands)
    manifest = json.loads((candidate / "scene_bridge_manifest.json").read_text())
    assert manifest["status"] == "awaiting_route_refinement"
    assert manifest["anchor_source_pts"] == [100, 200]
    assert fake_commands.stage_names == ["source_alignment", "solve_export", "target_sfm", "solve_alignment", "core_alignment"]
```

- [ ] **Step 2: Run runner test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridge_runner.py -q`
Expected: collection fails because the runner module is missing.

- [ ] **Step 3: Implement the runner with explicit existing commands**

```python
def _alignment_command(inputs, *, dataset, run_id, output_root, trajectory, sparse_ply, track, video):
    return (sys.executable, "-m", "cadscene.cli.run_pipeline", "--dataset", dataset,
            "--run-id", run_id, "--output-root", str(output_root), "--config",
            str(inputs.application_root / "configs/pipelines/sfm_overlay_existing_sfm.yaml"),
            "--stages", "alignment,viewer_scene", "--trajectory", str(trajectory),
            "--sparse-ply", str(sparse_ply), "--web-camera-track", str(track),
            "--video", str(video), "--cad-dir", str(inputs.cad_dir), "--cad-scale",
            str(inputs.cad_scale), "--origin-xy", *map(str, inputs.origin_xy))
```

The worker probes the source video once, writes an exact solve export manifest, runs the existing exporter and SfM command, builds the two solve anchors, runs solve alignment, derives two target-core anchors, and runs existing core alignment. It atomically writes progress for `solve_media`, `target_sfm`, `overlap_anchors`, `route_fit`, and `validated`.

- [ ] **Step 4: Write failing validation tests**

```python
def test_candidate_validation_rejects_duplicate_anchor_pts(tmp_path):
    candidate = _candidate(tmp_path, anchor_source_pts=[100, 100])
    result = validate_scene_bridge_candidate(candidate, candidate_identity())
    assert result.status == "failed"
    assert "distinct" in result.error

def test_candidate_validation_rejects_missing_core_alignment(tmp_path):
    candidate = _candidate(tmp_path)
    (candidate / "core_alignment/03_alignment/alignment.json").unlink()
    assert validate_scene_bridge_candidate(candidate, candidate_identity()).status == "failed"
```

- [ ] **Step 5: Implement strict candidate validation and CLI parser**

Validation checks the exact identity, two distinct PTS, seed sources and integer core frames, hashes, `alignment.json`, `sfm_camera_path.csv`, and `sfm_viewer_scene.json`. The CLI accepts only a JSON input path and writes errors through a nonzero exit code.

- [ ] **Step 6: Run runner/packaging tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridge_runner.py tests/packaging/test_wheel_contents.py -q`
Expected: PASS.

Commit: `git add cadscene/projects/scene_bridge_runner.py cadscene/cli/run_scene_bridge.py tests/projects/test_scene_bridge_runner.py tests/packaging/test_wheel_contents.py && git commit -m "feat: build validated scene bridge candidates"`

### Task 3: Queue orchestration and immutable publication

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `tests/projects/test_analysis_jobs.py`
- Modify: `tests/projects/test_executor.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Produces: `ProjectService.enqueue_scene_bridge(project_id: str, source_clip_id: str, target_clip_id: str, direction: str, *, expected_jobs_revision: int, expected_clips_revision: int) -> QueueJob`.
- Produces: job type `scene_bridge`, adapter `scene_bridge`, version `1`, immutable request at `projects/<project>/scene_bridge_requests/<operation_id>.json` and publication at `projects/<project>/scene_bridges/<target>/<revision>/`.
- HTTP: `POST /api/projects/<project>/clips/<source>/scene-bridges`.

- [ ] **Step 1: Write failing enqueue/idempotency/dependency tests**

```python
def test_scene_bridge_queues_target_core_trajectory_then_bridge(api):
    response = api.handle("POST", "/api/projects/project-1/clips/clip-2/scene-bridges", json_body=bridge_request(api, "down"))
    assert response.status == 202
    bridge = api.service.queue.get(response.body["job_id"])
    assert bridge.job_type == "scene_bridge"
    assert api.service.queue.get(bridge.depends_on_job_ids[-1]).job_type == "trajectory"

def test_repeated_current_scene_bridge_request_returns_same_job(api):
    first = enqueue_bridge(api)
    second = enqueue_bridge(api)
    assert second.body["job_id"] == first.body["job_id"]
```

- [ ] **Step 2: Run enqueue tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k scene_bridge -q`
Expected: endpoint returns 404.

- [ ] **Step 3: Implement bridge request identity, target dependencies, and execution plan**

```python
identity = {
    "schema_version": 1, "algorithm_version": "scene-overlap-v1",
    "project_id": project_id, "source_clip_id": source.clip_id,
    "target_clip_id": target.clip_id, "direction": direction,
    "source_analysis_revision": source.analysis_revision,
    "target_analysis_revision": target.analysis_revision,
    "source_workbench_output_revision": source_workbench.value["workbench_output_revision"],
    "source_workbench_output_fingerprint": source_workbench.value["workbench_output_fingerprint"],
    "overlap_seconds": "4",
}
```

The bridge job depends on the current or newly queued target core trajectory. `_current_input_fingerprint` recomputes identity from current project/CAD/video/clip/workbench inputs. `_build_job_execution_plan_locked` writes `scene_bridge_inputs.json` and invokes `cadscene.cli.run_scene_bridge`.

- [ ] **Step 4: Write failing stale/publication tests**

```python
def test_scene_bridge_publication_sets_only_active_lightweight_reference(service):
    finished = finish_bridge(service)
    target = clip(service, "clip-3")
    ref = next(ref for ref in target.references if ref.key == "scene_bridge:clip-3")
    assert ref.value["status"] == "awaiting_route_refinement"
    assert Path(ref.value["manifest_path"]).is_file()

def test_changed_source_workbench_revision_marks_completed_bridge_stale(service):
    job = running_bridge(service)
    replace_source_workbench_revision(service)
    assert finish_bridge_job(service, job).status == "stale_input"
    assert not active_bridge_reference(service, "clip-3")
```

- [ ] **Step 5: Implement validated immutable publication**

On success, validate the candidate, copy it through a same-parent temporary directory, fsync files/directories, `os.replace` into the revision directory, then update only the target clip reference. On validation failure or current-input mismatch, leave the target unchanged.

- [ ] **Step 6: Run service/API/executor tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_analysis_jobs.py tests/projects/test_executor.py tests/projects/test_workbench_sessions.py -k "scene_bridge or adjacent" -q`
Expected: PASS.

Commit: `git add cadscene/projects/service.py cadscene/projects/http_api.py tests/projects/test_analysis_jobs.py tests/projects/test_executor.py tests/projects/test_workbench_sessions.py && git commit -m "feat: orchestrate immutable scene bridge jobs"`

### Task 4: Workbench restoration and project workspace interaction

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/index.html`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Produces: `scene_bridge_capabilities(project_id, source_clip_id)` and `_restore_scene_bridge_to_run(project_id, clip)`.
- Snapshot adds `can_bridge_up`, `can_bridge_down`, target IDs, reason strings, and target `scene_bridge` job/progress/status.
- UI buttons read `向上打通` and `向下打通`; successful publication opens target, while later re-entry restores the same bridge at `workflowStage=keyframes`.

- [ ] **Step 1: Write failing capability and restore tests**

```python
def test_saved_sfm_route_can_bridge_same_scene_neighbors(api):
    source = snapshot_clip(api, "clip-2")
    assert source["capabilities"]["can_bridge_down"] is True
    assert source["capabilities"]["bridge_down_target_clip_id"] == "clip-3"

def test_open_target_restores_bridge_seed_and_alignment_before_session(api, runs_root):
    publish_bridge(api, source="clip-2", target="clip-3")
    opened = open_workbench(api, "clip-3")
    run = runs_root / "project-1-clip-3/clip-3"
    assert (run / "01_keyframes/camera_track_manual.json").is_file()
    assert (run / "03_alignment/alignment.json").is_file()
    assert "workflowStage=keyframes" in opened.body["workbench_url"]
```

- [ ] **Step 2: Run workbench tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k scene_bridge -q`
Expected: missing capability/restore behavior fails.

- [ ] **Step 3: Implement capability reasons and bridge restoration**

Restore first materializes the current core trajectory job, validates immutable bridge identity and hashes, writes the two-anchor manual track, and atomically replaces only bridge-owned `03_alignment`/viewer outputs. Existing saved/active target sessions continue to block bridge creation.

- [ ] **Step 4: Write failing static UI and polling tests**

```python
def test_workspace_exposes_scene_bridge_actions_and_single_progress_loop():
    script = workspace_script()
    assert "向上打通" in workspace_html()
    assert "向下打通" in workspace_html()
    assert "/scene-bridges" in script
    assert "waitForSceneBridge" in script
    assert "locate-adjacent" not in visible_bridge_handler(script)
```

- [ ] **Step 5: Implement UI request/poll/open flow**

The source-row buttons submit one scene-bridge request. The dialog reports a monotonic aggregate percentage; it stays below 100 until immutable publication. If the page remains visible, publication triggers one workbench-session request and navigation. If the user leaves, the target row displays `待微调` and normal entry restores it.

- [ ] **Step 6: Run workbench/UI tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py -k "scene_bridge or adjacent" -q`
Expected: PASS.

Commit: `git add cadscene/projects/workbench_sessions.py cadscene/projects/http_api.py apps/project_workspace/project_workspace.js apps/project_workspace/index.html tests/projects/test_workbench_sessions.py tests/viewer/test_project_workspace_static.py && git commit -m "feat: open bridged clips at route refinement"`

### Task 5: Core-frame invariants, compatibility, and real smoke

**Files:**
- Modify: `tests/projects/test_render_jobs.py`
- Modify: `tests/projects/test_concat.py`
- Modify: `tests/projects/test_concat_executor.py`
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `docs/superpowers/specs/2026-08-25-scene-overlap-route-bridging-design.md` only if implementation details need factual correction.

**Interfaces:**
- Verifies no production render/concat interface changes.

- [ ] **Step 1: Add failing regression assertions around a published bridge**

```python
def test_scene_bridge_never_changes_core_render_frames_or_ordinals(project_with_bridge):
    before = authoritative_core_map(project_with_bridge)
    publish_scene_bridge(project_with_bridge)
    after = authoritative_core_map(project_with_bridge)
    assert [(f.source_pts, f.output_ordinal) for f in after] == [
        (f.source_pts, f.output_ordinal) for f in before
    ]

def test_concat_partition_ignores_overlapping_solve_maps(project_with_bridge):
    result = build_final_frame_map(concat_plan(project_with_bridge), source_index(project_with_bridge))
    assert len({row["source_pts"] for row in result["frames"]}) == len(result["frames"])
```

- [ ] **Step 2: Run invariants and verify RED if any bridge data leaks into core paths**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/projects/test_concat_executor.py -k "scene_bridge or frame_map or partition" -q`
Expected: PASS after only test fixtures are completed; production concat code must remain unchanged.

- [ ] **Step 3: Run all focused suites**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_scene_bridges.py tests/projects/test_scene_bridge_runner.py tests/projects/test_workbench_sessions.py tests/projects/test_http_api.py tests/projects/test_executor.py tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/projects/test_concat_executor.py -q`
Expected: PASS.

- [ ] **Step 4: Run full verification**

Run: `python -m pytest -p no:cacheprovider`
Expected: all tests pass with only established skips.

Run: `python scripts/check_no_project_dependency.py`
Expected: success.

Run: `git diff --check`
Expected: no output.

- [ ] **Step 5: Perform real hygs smoke without overwriting the acceptance project**

Copy only manifests and writable outputs into a disposable project root, read the source video/CAD by path, bridge from completed hygs `clip-0003` to one same-scene neighbor, and verify: two distinct shared PTS; published immutable revision; target opens at keyframes with alignment present; no quality artifact appears; core frame map hash is unchanged.

- [ ] **Step 6: Commit verification adjustments**

Commit: `git add tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/projects/test_concat_executor.py tests/projects/test_workbench_sessions.py docs/superpowers/specs/2026-08-25-scene-overlap-route-bridging-design.md && git commit -m "test: verify scene bridge preserves core frame boundaries"`

## Self-review

- Spec coverage: tasks cover exact solve/core ranges, two shared PTS, existing alignment reuse, background queue progress, immutable publication/staleness, target route refinement, refresh recovery, rejection cases, and render/concat invariants.
- Placeholder scan: no TBD/TODO or unspecified error-handling steps remain.
- Type consistency: `SolveInterval`, `SceneBridgeAnchor`, `SceneBridgeInputs`, `enqueue_scene_bridge`, scene-bridge reference keys and job type are named consistently across tasks.
