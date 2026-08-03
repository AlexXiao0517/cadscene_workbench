# Video Project Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the Stage 8A–8E single-machine upload, analysis, clip management, bounded trajectory queue, workbench round trip, per-clip render, and verified source-order concat loop.

**Architecture:** Keep `serve_viewer` as a thin HTTP entry point and add a focused `cadscene.projects` domain with repository, scheduler, adapter, upload, workbench, and media boundaries. Persist four atomically replaced JSON manifests while adapters write only immutable attempt outputs and return structured results.

**Tech Stack:** Python 3.11+, dataclasses/protocols, stdlib HTTP server and subprocesses, FFmpeg/FFprobe, NumPy/OpenCV where already used, native HTML/CSS/JavaScript, pytest.

## Global Constraints

- Base all work on `feature/video-analysis-segmentation@c907b3e`, which includes Pure Rotation ancestor `825b806`.
- Decoded-frame integer PTS in presentation order plus exact time base is authoritative; packet timestamps are diagnostic only.
- All clip ranges are `[source_start_pts, source_end_pts_exclusive)` and every clip is strictly shorter than 60 seconds.
- Do not modify the mathematical internals of `sfm_only`, `srt_sfm_fused`, `srt_full_pose`, or `pure_rotation`.
- All manifest mutations require `expected_revision`, process locks, same-directory temporary writes, fsync, and atomic replace.
- Adapters never write manifests and never invent progress percentages; they report structured stages and optional measured progress.
- Long-video uploads publish only after size, fingerprint, decoding, and integrity validation.
- Current deployment is one machine and one `serve_viewer` process per project root.

---

### Task 1: Repair Stage 8A frame ownership and physical export

**Files:**
- Modify: `cadscene/video_analysis/pts.py`
- Modify: `cadscene/video_analysis/models.py`
- Modify: `cadscene/video_analysis/segmentation.py`
- Modify: `cadscene/video_analysis/analyzer.py`
- Modify: `cadscene/video_analysis/clip_export.py`
- Test: `tests/video_analysis/test_pts.py`
- Test: `tests/video_analysis/test_segmentation.py`
- Test: `tests/video_analysis/test_analyzer.py`
- Test: `tests/video_analysis/test_clip_export.py`

**Interfaces:**
- Produces: `DecodedFrameTimestamp`, `DecodedFrameIndex`, `probe_decoded_frame_index()`, `frames_for_interval()`, `validate_frame_partition()`, and integer-PTS `ExportClip`.
- Consumes: existing analysis boundaries, sparse decoded images, FFmpeg resolver, and atomic directory publication.

- [ ] **Step 1: Add failing decoded-frame index tests**

```python
def test_frame_index_prefers_pts_then_best_effort_in_presentation_order():
    frames = parse_decoded_frame_records(FRAME_JSON, time_base=Fraction(1, 1000))
    assert [frame.pts for frame in frames] == [5000, 5040, 5080]
    assert frames[1].timestamp_source == "best_effort_timestamp"

def test_source_end_exclusive_includes_last_frame():
    index = DecodedFrameIndex(Fraction(1, 1000), FRAMES)
    assert index.source_end_pts_exclusive == 5120
    assert index.frames[-1].pts < index.source_end_pts_exclusive
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `pytest -q tests/video_analysis/test_pts.py -k "frame_index or source_end_exclusive"`  
Expected: fail because decoded-frame index APIs do not exist.

- [ ] **Step 3: Implement the decoded-frame index**

```python
@dataclass(frozen=True)
class DecodedFrameTimestamp:
    ordinal: int
    pts: int
    duration_pts: int | None
    timestamp_source: str

@dataclass(frozen=True)
class DecodedFrameIndex:
    time_base: Fraction
    frames: tuple[DecodedFrameTimestamp, ...]
    source_start_pts: int
    source_end_pts_exclusive: int
```

Use `ffprobe -show_frames -show_entries frame=pts,best_effort_timestamp,pkt_duration -of json`, preserve returned presentation order, and record packet diagnostics separately.

- [ ] **Step 4: Add failing hard-cut, black-transition, non-zero-start, and final-frame tests**

```python
def test_hard_cut_first_new_scene_frame_belongs_only_to_next_clip():
    clips = segment_video(FRAME_INDEX, boundaries=[Boundary(pts=9000, reason="hard_cut")])
    assert clips[0].source_end_pts_exclusive == 9000
    assert clips[1].source_start_pts == 9000
    assert 9000 not in frames_for_interval(FRAME_INDEX, clips[0])
    assert 9000 in frames_for_interval(FRAME_INDEX, clips[1])
```

Run: `pytest -q tests/video_analysis/test_segmentation.py tests/video_analysis/test_analyzer.py`  
Expected: fail on float-second ownership and missing exclusive fields.

- [ ] **Step 5: Implement integer-PTS half-open segmentation**

Snap scene candidates to real decoded-frame PTS, assign short black frames to the preceding scene, derive scene/segment indices, and choose the minimum number of real-frame cuts required for strict `<60s` clips.

- [ ] **Step 6: Add failing exporter and frame-map tests**

```python
def test_export_command_uses_absolute_pts_trim_and_passthrough(tmp_path):
    command = build_export_command(CLIP, tmp_path / "clip.mp4")
    assert "trim=start_pts=5000:end_pts=9000" in command
    assert "-fps_mode" in command and "passthrough" in command
    assert "-ss" not in command and "-t" not in command

def test_adjacent_exports_partition_source_frames_once():
    assert concatenate_maps(export_maps(CLIPS)) == list(FRAME_INDEX.frames)
```

Run: `pytest -q tests/video_analysis/test_clip_export.py`  
Expected: fail because export still uses float `-ss/-t` and has no sidecar.

- [ ] **Step 7: Implement frame-exact export and validation**

Create `clip_frame_map.json` before encoding, use absolute-PTS `trim` plus `setpts`, `fps_mode passthrough`, separately trim optional audio, and validate output count against the sidecar before atomic publication.

- [ ] **Step 8: Run focused and Stage 8A regression tests**

Run: `pytest -q tests/video_analysis`  
Expected: all Stage 8A tests pass.

- [ ] **Step 9: Commit**

```bash
git add cadscene/video_analysis tests/video_analysis
git commit -m "fix: enforce half-open decoded-frame clip boundaries"
```

### Task 2: Add project manifests and atomic repositories

**Files:**
- Create: `cadscene/projects/__init__.py`
- Create: `cadscene/projects/models.py`
- Create: `cadscene/projects/repositories.py`
- Create: `cadscene/projects/json_repositories.py`
- Create: `cadscene/projects/recovery.py`
- Test: `tests/projects/test_models.py`
- Test: `tests/projects/test_json_repositories.py`
- Test: `tests/projects/test_recovery.py`

**Interfaces:**
- Produces: four manifest dataclasses, `ManifestRepository[T]`, `AtomicJsonRepository[T]`, `RevisionConflict`, and `reconcile_project()`.
- Consumes: `LogicalClip.to_dict()` and immutable analysis revisions from Task 1.

- [ ] **Step 1: Write failing schema and layered-value tests**

```python
def test_custom_name_and_workflow_override_survive_analysis_refresh():
    refreshed = reconcile_clips(existing=USER_EDITED, candidate=NEW_ANALYSIS)
    assert refreshed.custom_display_name == "东侧入口"
    assert refreshed.display_name == "东侧入口"
    assert refreshed.workflow_override == "pure_rotation"
```

Run: `pytest -q tests/projects/test_models.py`  
Expected: import failure because the project domain does not exist.

- [ ] **Step 2: Implement project, clip, job, and render models**

```python
@dataclass(frozen=True)
class ManifestHeader:
    schema_version: str
    revision: int
    updated_at: str
    project_id: str

@dataclass(frozen=True)
class ClipWorkflow:
    recommended_workflow: str | None
    workflow_override: str | None
    resolved_workflow: str | None
```

Store `generated_display_name`, nullable `custom_display_name`, and resolved `display_name`; permanent `clip_id` owns all references.

- [ ] **Step 3: Write failing expected-revision, lock, atomic-write, and operation recovery tests**

```python
def test_update_rejects_stale_expected_revision(repository):
    repository.update("p1", expected_revision=2, mutate=lambda value: value)
    with pytest.raises(RevisionConflict):
        repository.update("p1", expected_revision=2, mutate=lambda value: value)
```

Run: `pytest -q tests/projects/test_json_repositories.py tests/projects/test_recovery.py`  
Expected: fail because repositories are absent.

- [ ] **Step 4: Implement repositories and recovery**

Use per-path `threading.RLock`, fixed multi-file lock order, operation IDs, same-directory temp files, file fsync, `os.replace`, and startup reconciliation without claiming transaction semantics.

- [ ] **Step 5: Run project-domain tests and commit**

Run: `pytest -q tests/projects/test_models.py tests/projects/test_json_repositories.py tests/projects/test_recovery.py`  
Expected: pass.

```bash
git add cadscene/projects tests/projects
git commit -m "feat: add atomic project manifest repositories"
```

### Task 3: Add the bounded queue and workflow adapters

**Files:**
- Create: `cadscene/projects/queue.py`
- Create: `cadscene/projects/adapters.py`
- Create: `cadscene/projects/workflow_adapters.py`
- Create: `cadscene/projects/service.py`
- Test: `tests/projects/test_queue.py`
- Test: `tests/projects/test_workflow_adapters.py`
- Test: `tests/projects/test_service_jobs.py`

**Interfaces:**
- Produces: `TaskQueue`, `LocalResourceQueue`, `AdapterProgress`, `AdapterResult`, `WorkflowAdapter`, and `ProjectService.enqueue_trajectory_jobs()`.
- Consumes: repositories from Task 2 and existing CLI command builders/job runner behavior.

- [ ] **Step 1: Write failing dependency, capacity, exclusive-key, and partial-preflight tests**

```python
def test_heavy_jobs_run_one_at_a_time_by_default(queue):
    queue.submit(heavy("a"))
    queue.submit(heavy("b"))
    assert queue.running_ids() == ["a"]
    assert queue.status("b") == "queued"

def test_dependency_must_be_validated_success(queue):
    queue.submit(job("solve", depends_on_job_ids=("export",)))
    assert queue.status("solve") == "queued"
```

Run: `pytest -q tests/projects/test_queue.py tests/projects/test_service_jobs.py`  
Expected: fail because queue APIs do not exist.

- [ ] **Step 2: Implement static resource scheduling and structured progress**

```python
@dataclass(frozen=True)
class AdapterProgress:
    stage: str
    message: str
    fraction: float | None = None
```

Default heavy/light/media capacities are one. A missing fraction produces a stage-only UI update.

- [ ] **Step 3: Write failing four-adapter routing and stale-input tests**

```python
@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_resolved_workflow_selects_existing_adapter(workflow, registry):
    assert registry.for_workflow(workflow).name == workflow

def test_changed_input_marks_active_job_stale_input(service):
    assert service.finish_job(OLD_RESULT, current_fingerprint="new").status == "stale_input"
```

- [ ] **Step 4: Implement adapters without algorithm changes**

Wrap existing commands and output validators. Adapters write only attempt directories and return `AdapterResult`; `ProjectService` alone updates manifests.

- [ ] **Step 5: Add process-tree cancellation and restart tests, then implement**

Verify cancellation terminates descendants and preserves attempt logs. Restore queued order; convert unverifiable running jobs to interrupted.

- [ ] **Step 6: Run related tests and commit**

Run: `pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/workflow tests/pure_rotation`  
Expected: pass.

```bash
git add cadscene/projects tests/projects
git commit -m "feat: add bounded project workflow queue"
```

### Task 4: Add validated upload, project API, and workspace page

**Files:**
- Create: `cadscene/projects/uploads.py`
- Create: `cadscene/projects/http_api.py`
- Create: `apps/project_workspace/index.html`
- Create: `apps/project_workspace/style.css`
- Create: `apps/project_workspace/project_workspace.js`
- Modify: `cadscene/cli/serve_viewer.py`
- Modify: `apps/workflow_portal/workflow_portal.js`
- Test: `tests/projects/test_uploads.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Produces: `ValidatedUploadStore`, `ProjectApi`, snapshot ETag/capabilities, and the project workspace.
- Consumes: `ProjectService`, repositories, and queue from Tasks 2–3.

- [ ] **Step 1: Write failing interrupted-upload and atomic-publication tests**

```python
def test_interrupted_upload_never_becomes_analyzable(store):
    pending = store.begin("p1", "video", expected_size=100)
    pending.write(b"short")
    pending.abort()
    assert not store.published_path("p1", "video").exists()
```

Run: `pytest -q tests/projects/test_uploads.py`  
Expected: fail because upload store does not exist.

- [ ] **Step 2: Implement temporary upload, SHA-256, size, decode, and atomic publish**

Only the published path may trigger CAD/video analysis. Preserve validation reports and reject corrupt or truncated media.

- [ ] **Step 3: Write failing API revision, ETag/304, capability, and batch-preflight tests**

```python
def test_unchanged_snapshot_returns_304_without_body(client):
    first = client.get("/api/projects/p1/snapshot")
    second = client.get("/api/projects/p1/snapshot", headers={"If-None-Match": first.etag})
    assert second.status == 304 and second.body == b""
```

- [ ] **Step 4: Implement thin routing and project API delegation**

Move project route parsing/validation into `ProjectApi`; keep `RangeRequestHandler` limited to dispatch and response serialization.

- [ ] **Step 5: Write failing static UI contract tests, then build the workspace**

Assert the collapsible icon sidebar contains 项目概览、项目文件、片段管理、设置; clip labels use scene/segment names and friendly time; recommendation is normal text; state and progress are separate; only one merge action exists.

- [ ] **Step 6: Implement safe polling and upload redirect**

Poll with ETag every one to two seconds, preserve local dirty edits and selection, use server capabilities for controls, and enter the project page immediately after uploads publish.

- [ ] **Step 7: Run API/UI/upload tests and commit**

Run: `pytest -q tests/projects/test_uploads.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_project_workspace_static.py tests/cli/test_serve_viewer_upload_api.py tests/viewer`  
Expected: pass.

```bash
git add cadscene/projects cadscene/cli/serve_viewer.py apps/project_workspace apps/workflow_portal tests
git commit -m "feat: add upload-driven project workspace"
```

### Task 5: Add workbench session save and return

**Files:**
- Create: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Produces: `WorkbenchSessionStore`, `create_workbench_session()`, `save_workbench_output()`, and immutable `workbench_output_revision`.
- Consumes: adapter workbench descriptors and project repositories.

- [ ] **Step 1: Write failing binding, expiry, allowlist, and immutable-save tests**

```python
def test_session_rejects_changed_clip_input(coordinator):
    session = coordinator.create(BOUND_INPUT)
    with pytest.raises(StaleWorkbenchSession):
        coordinator.save(session.token, CURRENT_CHANGED_INPUT, OUTPUT)
```

Run: `pytest -q tests/projects/test_workbench_sessions.py`  
Expected: fail because coordinator does not exist.

- [ ] **Step 2: Implement workbench session coordination**

Bind project, clip, workflow, input revision/fingerprint, run, permissions, expiry, and same-origin allowlisted return path. Validate adapter output before creating an immutable save revision.

- [ ] **Step 3: Add UI round-trip tests and implementation**

Pass the session token into the existing workbench, call the coordination save endpoint after the existing save, return to and focus the originating clip, and turn expired unsaved editing back to ready.

- [ ] **Step 4: Run workbench and workflow regressions and commit**

Run: `pytest -q tests/projects/test_workbench_sessions.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_workflow_ui_static.py tests/workflow tests/pure_rotation`  
Expected: pass.

```bash
git add cadscene/projects apps/web_camera_viewer apps/project_workspace tests
git commit -m "feat: restore project state across workbench saves"
```

### Task 6: Add standard-media clip render and verified concat

**Files:**
- Create: `cadscene/projects/media.py`
- Create: `cadscene/projects/render_adapters.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/projects/test_media.py`
- Test: `tests/projects/test_render_jobs.py`
- Test: `tests/projects/test_concat.py`

**Interfaces:**
- Produces: `ProjectMediaSpec`, `probe_media()`, `validate_render_frame_map()`, `normalize_segment()`, and `concat_project()`.
- Consumes: verified clip/workbench outputs, integer-PTS source fallback, queue, and render manifest.

- [ ] **Step 1: Write failing project media-spec and one-frame-in/one-frame-out tests**

```python
def test_render_count_must_equal_frame_map(media_validator):
    with pytest.raises(FrameMapMismatch):
        media_validator.validate(rendered_frame_count=99, frame_map=FRAME_MAP_100)
```

- [ ] **Step 2: Implement media probing and render validation**

Define width, height, baked orientation, SAR, pixel format, codec/profile, time base, and color metadata. Publish only zero-start, monotonic, non-negative outputs with exact frame counts.

- [ ] **Step 3: Write failing fallback, normalization, concat-map, and original-audio tests**

```python
def test_final_map_equals_complete_source_sequence(concat_result):
    assert concat_result.frame_map == SOURCE_FRAME_INDEX.pts_sequence

def test_merge_muxes_original_complete_audio(concat_command):
    assert concat_command.audio_source == ORIGINAL_LONG_VIDEO
```

- [ ] **Step 4: Implement per-clip render, explicit fallback, normalization, and concat**

Render directly to the project media spec where possible. Normalize incompatible video with passthrough frame timing, concat source-order segments, then mux the original long-video audio using the mapped final video duration.

- [ ] **Step 5: Validate audio/video tolerance and stale-input behavior**

Set `audio_video_duration_tolerance_sec` to
`max(0.050, max_source_frame_duration_sec)` and record the measured delta and
limit in the validation report. Input revision changes produce
`superseded/stale_input`, retain diagnostics, and publish nothing.

- [ ] **Step 6: Run media/render tests and commit**

Run: `pytest -q tests/projects/test_media.py tests/projects/test_render_jobs.py tests/projects/test_concat.py tests/rendering tests/pure_rotation`  
Expected: pass.

```bash
git add cadscene/projects apps/project_workspace tests/projects
git commit -m "feat: render and concatenate verified project clips"
```

### Task 7: Verify the real project loop and full regression

**Files:**
- Create: `tests/projects/test_project_e2e.py`
- Create: `scripts/validate_video_project.py`
- Modify: `docs/technical/api-and-artifacts.md`
- Modify: `docs/web_viewer_usage.md`

**Interfaces:**
- Produces: reproducible real-project validation command and final test/performance evidence.
- Consumes: all Stage 8A–8E public APIs and artifacts.

- [ ] **Step 1: Add an end-to-end synthetic project test**

Create/upload synthetic CAD/video inputs, wait for bounded jobs, change/reset a workflow override, simulate workbench save, render, source-fallback one clip, merge, and assert final frame-map equality.

- [ ] **Step 2: Run `jinhuaorigin.mp4` validation**

Run the CLI against the repository’s current real video, verify all seven known scene changes near 01:29, 02:22, 03:35, 04:03, 04:33, 05:54, and 06:40, and record clips, modes, recommendations, review flags, wall time, and peak resources.

- [ ] **Step 3: Run focused and related suites**

Run: `pytest -q tests/video_analysis tests/projects tests/cli/test_serve_viewer_project_api.py tests/viewer tests/workflow tests/pure_rotation tests/rendering`  
Expected: pass.

- [ ] **Step 4: Run the full suite**

Run: `pytest -q`  
Expected: no regression relative to the clean baseline.

- [ ] **Step 5: Confirm repository isolation and commit**

Verify the integration worktree is clean after the commit and the original main worktree remains at its pre-work state.

```bash
git add tests/projects scripts/validate_video_project.py docs/technical/api-and-artifacts.md docs/web_viewer_usage.md
git commit -m "test: verify complete video project pipeline"
```
