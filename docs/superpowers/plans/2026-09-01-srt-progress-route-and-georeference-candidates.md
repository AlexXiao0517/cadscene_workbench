# SRT Progress, Full-Pose Route, and Georeference Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SRT parsing visible during project creation, explain why complete DJI SRT selects full pose, and add a user-entered central meridian with durable real-progress CAD coordinate candidate generation.

**Architecture:** Keep SRT parsing inside `video_analysis`, where authoritative PTS and clip intervals already exist, and expose it as explicit progress stages. Add a project-level `cad_georeference_candidates` queue job whose immutable request and output are fingerprinted against the current CAD, SRT, central meridian, and algorithm version; the workspace starts and polls that job through the project API.

**Tech Stack:** Python 3.11+, dataclasses, existing JSON repositories and `LocalResourceQueue`, `pyproj`/PROJ, vanilla HTML/CSS/JavaScript, pytest.

## Global Constraints

- `srt_full_pose` keeps the existing 80% joint GPS/height/gimbal coverage threshold and never runs SfM.
- `srt_sfm_fused` remains an experimental/interface-only route for usable position with incomplete gimbal attitude.
- Horizontal FOV and CGCS2000 central meridian remain separate numeric values.
- A user central meridian filters auditable EPSG candidates; it never auto-confirms a CRS and never creates an ad-hoc projection string.
- Progress fractions must come from known work counts; unknown-duration stages are indeterminate.
- Candidate results and confirmations are invalid when their CAD/SRT/input fingerprint is stale.

---

### Task 1: Expose SRT parsing and route-selection progress

**Files:**
- Modify: `cadscene/video_analysis/analyzer.py`
- Modify: `tests/video_analysis/test_analyzer.py`

**Interfaces:**
- `analyze_video(..., progress_callback=...)` emits `parsing_srt` before `analyze_srt_stream` and `routing_clips` before per-clip `assess_clip_srt_coverage`/`recommend_workflow`.
- Both stages use the existing `Callable[[str, str, float | None], None]` callback and preserve current artifact schemas.

- [ ] **Step 1: Write the failing analyzer progress test**

Add a test using the existing analyzer stubs and a full-pose SRT fixture:

```python
def test_analyze_video_reports_srt_parse_before_route_selection(tmp_path, monkeypatch):
    events: list[tuple[str, str, float | None]] = []
    _install_analyzer_stubs(monkeypatch)
    analyze_video(
        video_path=tmp_path / "flight.mp4",
        srt_path=_write_full_pose_srt(tmp_path),
        output_root=tmp_path / "out",
        project_id="project-1",
        progress_callback=lambda stage, message, fraction: events.append(
            (stage, message, fraction)
        ),
    )
    stages = [stage for stage, _, _ in events]
    assert stages.index("parsing_srt") < stages.index("routing_clips")
    assert stages.index("routing_clips") < stages.index("publishing")
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/video_analysis/test_analyzer.py -k "srt_parse_before_route" -q`

Expected: FAIL because neither progress stage is emitted.

- [ ] **Step 3: Add the minimal progress reports**

Immediately before reading SRT:

```python
if srt_path is not None:
    report("parsing_srt", "正在解析 SRT 飞行与云台姿态", None)
```

After SRT records are loaded and before iterating `planned`:

```python
if srt_path is not None:
    report("routing_clips", "正在按片段评估 SRT 姿态覆盖率", 0.91)
```

Do not emit these stages when there is no SRT.

- [ ] **Step 4: Run analyzer and recommendation tests**

Run: `python -m pytest -p no:cacheprovider tests/video_analysis/test_analyzer.py tests/video_analysis/test_recommendation.py tests/srt/test_capability.py -q`

Expected: PASS, including existing full-pose and partial-pose recommendation assertions.

- [ ] **Step 5: Commit**

Run: `git add cadscene/video_analysis/analyzer.py tests/video_analysis/test_analyzer.py && git commit -m "feat: report SRT parsing during video analysis"`

### Task 2: Render a conditional SRT stage and explicit workflow names

**Files:**
- Modify: `apps/workflow_portal/index.html`
- Modify: `apps/workflow_portal/workflow_portal.js`
- Modify: `apps/workflow_portal/style.css`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `tests/viewer/test_workflow_portal_static.py`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- `snapshot.assets.srt` is the durable source of whether the progress modal contains an SRT stage.
- `renderAnalysis` derives SRT completion from the `video_analysis` progress stage: earlier stages are pending, `parsing_srt` is active/indeterminate, and `routing_clips` or later is complete.
- Workflow display strings are centralized in the workspace script.

- [ ] **Step 1: Add failing static UI contract tests**

```python
def test_project_creation_progress_has_conditionally_hidden_srt_stage():
    html = _portal_html()
    script = _portal_script()
    assert 'id="taskStageSrt"' in html
    assert 'snapshot.assets?.srt' in script
    assert 'stage === "parsing_srt"' in script

def test_workspace_distinguishes_full_pose_from_experimental_fusion():
    script = _workspace_script()
    assert "SRT 全姿态（跳过三维重建）" in script
    assert "SRT 定位 + 三维重建（实验）" in script
```

- [ ] **Step 2: Run static tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py -k "srt_stage or distinguishes_full_pose" -q`

Expected: FAIL because the SRT row and clarified labels are absent.

- [ ] **Step 3: Implement conditional stage state**

Add an initially hidden SRT `<li>` between CAD and video. Update stage numbers dynamically from the visible ordered rows. Compute video/SRT progress using a declared stage order:

```javascript
const VIDEO_STAGE_ORDER = [
  "probing_pts", "sampling_frames", "verifying_scenes", "segmenting",
  "parsing_srt", "routing_clips", "publishing", "complete",
];
```

Use an `is-indeterminate` class for active stages without a fraction. Recalculate overall progress from the visible stage weights and keep the SRT row hidden when `snapshot.assets?.srt` is absent.

- [ ] **Step 4: Centralize clarified workflow labels**

Add a mapping used by selects, status text, and dialog headings:

```javascript
const WORKFLOW_LABELS = {
  srt_full_pose: "SRT 全姿态（跳过三维重建）",
  srt_sfm_fused: "SRT 定位 + 三维重建（实验）",
  sfm_only: "三维重建",
};
```

Render available `clip.srt_coverage` values as a concise reason near the route controls rather than recomputing capability in JavaScript.

- [ ] **Step 5: Run static tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py -q`

Expected: PASS.

Run: `git add apps/workflow_portal apps/project_workspace/project_workspace.js tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py && git commit -m "feat: explain SRT progress and full-pose routing"`

### Task 3: Filter georeference candidates and report real scoring progress

**Files:**
- Modify: `cadscene/srt/georeference.py`
- Modify: `tests/srt/test_georeference.py`

**Interfaces:**
- Extend `recommend_cgcs2000_candidates(..., central_meridian_deg: float | None = None, progress_callback: Callable[[str, str, float | None], None] | None = None)`.
- Callback stages are `validating_inputs`, `enumerating_crs`, `scoring_candidates`, `building_previews`, and `complete`.
- `scoring_candidates` fraction is based on processed `(CRS, axis_mapping)` variants divided by the known total.

- [ ] **Step 1: Write failing central-meridian and progress tests**

```python
def test_manual_120_meridian_filters_candidates():
    candidates = recommend_cgcs2000_candidates(
        LONGITUDES, LATITUDES, CAD_BBOX,
        central_meridian_deg=120.0,
    )
    assert candidates
    assert {item.central_meridian_deg for item in candidates} == {120.0}

def test_candidate_progress_uses_processed_variant_count():
    events = []
    recommend_cgcs2000_candidates(
        LONGITUDES, LATITUDES, CAD_BBOX,
        central_meridian_deg=120.0,
        progress_callback=lambda stage, message, fraction: events.append(
            (stage, message, fraction)
        ),
    )
    scoring = [fraction for stage, _, fraction in events if stage == "scoring_candidates"]
    assert scoring and scoring[-1] == 1.0
    assert [stage for stage, _, _ in events][-1] == "complete"
```

Also test non-finite/out-of-range meridians and a meridian with no matching EPSG.

- [ ] **Step 2: Run georeference tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -k "manual_120 or candidate_progress or invalid_meridian" -q`

Expected: FAIL because the new parameters do not exist.

- [ ] **Step 3: Implement filtering before projection**

Validate the optional meridian, enumerate CRS info once, resolve each CRS central meridian, and filter with an absolute tolerance of `1e-9`. Build the complete `(info, mapping)` work list before scoring so the denominator is real. Raise `ValueError("no CGCS2000 Gauss-Kruger EPSG candidate matches central meridian ...")` when a manual meridian matches none.

- [ ] **Step 4: Emit progress from actual work**

Call the progress callback only at state transitions and after each scored variant. Keep auto mode sorting by distance to median SRT longitude. Do not sleep or synthesize intermediate fractions.

- [ ] **Step 5: Run georeference tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -q`

Expected: PASS.

Run: `git add cadscene/srt/georeference.py tests/srt/test_georeference.py && git commit -m "feat: filter and report CRS candidate scoring"`

### Task 4: Add durable candidate-generation jobs and API polling

**Files:**
- Create: `cadscene/cli/build_cad_georeference_candidates.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `tests/cli/test_build_cad_georeference_candidates_cli.py`
- Modify: `tests/projects/test_analysis_jobs.py`
- Modify: `tests/cli/test_serve_viewer_project_api.py`
- Modify: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- `ProjectService.enqueue_cad_georeference_candidates(project_id, *, expected_revision, central_meridian_deg, limit=6) -> QueueJob`.
- `ProjectService.cad_georeference_candidate_status(project_id, *, central_meridian_deg) -> Mapping[str, object]` returns status, stale, progress, error, and validated candidates.
- CLI consumes an immutable request JSON and writes `cad_georeference_candidates.json` plus existing `adapter_progress.json` sidecar.
- The job adapter name/version are `cad_georeference_candidates`/`1` and output validation binds the result fingerprint to the job input fingerprint.

- [ ] **Step 1: Write failing CLI red tests**

```python
def test_candidate_cli_publishes_fingerprinted_candidates_and_progress(tmp_path):
    result = run_cli(_request_json(tmp_path), tmp_path / "attempt")
    payload = json.loads((tmp_path / "attempt" / "cad_georeference_candidates.json").read_text())
    progress = json.loads((tmp_path / "attempt" / "adapter_progress.json").read_text())
    assert result == 0
    assert payload["input_fingerprint"] == REQUEST_FINGERPRINT
    assert payload["central_meridian_deg"] == 120.0
    assert progress["stage"] == "complete"
    assert progress["fraction"] == 1.0
```

- [ ] **Step 2: Run CLI test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_build_cad_georeference_candidates_cli.py -q`

Expected: FAIL because the CLI module is missing.

- [ ] **Step 3: Implement atomic CLI output**

Load only copied immutable request inputs, call `recommend_cgcs2000_candidates` with a callback that atomically rewrites `adapter_progress.json`, and atomically replace the final candidate JSON. Include schema version, algorithm version, input fingerprint, requested meridian, candidates, and generated timestamp.

- [ ] **Step 4: Write failing service/API lifecycle tests**

Cover enqueue, idempotent reuse, queued/running/success payloads, central-meridian fingerprint changes, CAD/SRT replacement staleness, revision conflict, failed retry, corrupted output rejection, and confirmation rejection for candidates not belonging to the current successful job.

API expectations:

```python
started = api.handle("POST", path, json_body={
    "expected_revision": project.revision,
    "central_meridian_deg": 120.0,
})
assert started.status == 202
polled = api.handle("GET", path)
assert polled.body["operation"]["job_type"] == "cad_georeference_candidates"
```

- [ ] **Step 5: Wire the job through the existing executor**

Add the job to `_build_job_execution_plan_locked`, `_current_input_fingerprint`, output validation/publication, visible progress, retry, and recovery branches that are required for an executable project-level job. Materialize CAD bbox and sampled WGS84 positions into an immutable request JSON inside the attempt; the worker must not reread mutable project manifests.

- [ ] **Step 6: Replace the synchronous API**

POST accepts `expected_revision` and optional `central_meridian_deg`, returns 202 operation state, and does not block on PROJ scoring. GET returns the latest candidate operation together with its requested meridian and input fingerprint; the client compares those fields with the current input before exposing candidates. Confirmation requires the current candidate job ID/fingerprint in addition to the candidate payload.

- [ ] **Step 7: Verify focused lifecycle and packaging tests**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_build_cad_georeference_candidates_cli.py tests/projects/test_analysis_jobs.py tests/cli/test_serve_viewer_project_api.py tests/packaging/test_wheel_contents.py -k "georeference_candidate" -q`

Expected: PASS.

- [ ] **Step 8: Commit**

Run: `git add cadscene/cli/build_cad_georeference_candidates.py cadscene/projects/service.py cadscene/projects/http_api.py tests/cli/test_build_cad_georeference_candidates_cli.py tests/projects/test_analysis_jobs.py tests/cli/test_serve_viewer_project_api.py tests/packaging/test_wheel_contents.py && git commit -m "feat: run CAD georeference candidates as durable jobs"`

### Task 5: Add central-meridian controls and recoverable candidate progress

**Files:**
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `apps/project_workspace/style.css`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Input ID `centralMeridianInput`; blank means auto, otherwise POST a finite number.
- Progress container IDs `cadGeoreferenceProgress`, `cadGeoreferenceProgressFill`, `cadGeoreferenceProgressText`, and `cadGeoreferenceProgressPercent`.
- Polling resumes while operation status is `queued`, `running`, or `cancelling`; stale results are never passed to confirmation.

- [ ] **Step 1: Write failing workspace contracts**

```python
def test_full_pose_dialog_has_distinct_fov_and_central_meridian_inputs():
    html = _workspace_html()
    assert 'id="horizontalFovInput"' in html
    assert 'id="centralMeridianInput"' in html
    assert "CGCS2000 中央经线（°）" in html

def test_candidate_request_posts_meridian_and_polls_real_operation():
    script = _workspace_script()
    assert "central_meridian_deg" in script
    assert "cadGeoreferenceProgress" in script
    assert 'method: "GET"' in script
    assert "renderCadGeoreferenceOperation" in script
```

- [ ] **Step 2: Run static tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -k "central_meridian or candidate_request" -q`

Expected: FAIL because the field and operation UI do not exist.

- [ ] **Step 3: Implement input, operation rendering, and polling**

POST the current project revision and normalized meridian. Render fraction only when finite; otherwise add the existing indeterminate progress class. Poll through the existing request/delay pattern, stop on terminal state or when the dialog closes, and restore a running operation when the dialog reopens.

- [ ] **Step 4: Preserve explicit confirmation and stale protection**

Confirmation POST includes `candidate_job_id` and `candidate_input_fingerprint`. Disable candidate cards while stale/running/failed. Changing the central meridian clears the current selection and labels previously shown candidates as obsolete without deleting the server audit record.

- [ ] **Step 5: Run workspace tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -q`

Expected: PASS.

Run: `git add apps/project_workspace tests/viewer/test_project_workspace_static.py && git commit -m "feat: configure central meridian with candidate progress"`

### Task 6: End-to-end verification, documentation, and demo service

**Files:**
- Modify: `tests/integration/test_srt_full_pose_workflow.py`
- Modify: `README.md`
- Modify: `docs/workflow_routing.md`
- Modify: `docs/technical/developer-guide.md`
- Modify: `docs/technical/troubleshooting.md`

**Interfaces:**
- Integration fixture covers project creation with CAD/video/SRT, complete full-pose route, manual 120° candidate operation, explicit confirmation, FOV save, and no SfM command.

- [ ] **Step 1: Add the failing integration contract**

Extend the existing full-pose integration test to assert that candidate generation is asynchronous, reports at least one scoring stage, completes with matching-fingerprint candidates, and can be confirmed before full-pose execution.

- [ ] **Step 2: Run integration test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/integration/test_srt_full_pose_workflow.py -q`

Expected: FAIL until all API and job wiring is present.

- [ ] **Step 3: Complete only the missing integration wiring**

Fix production behavior only for the failing end-to-end contract. Do not add support for production execution of `srt_sfm_fused`, automatic CRS confirmation, variable FOV, or custom projection strings.

- [ ] **Step 4: Update user and developer documentation**

Document conditional SRT progress, clarified route names, the distinction between horizontal FOV and central meridian, candidate job stages, recovery, retry, and stale-input behavior.

- [ ] **Step 5: Run focused suites**

Run: `python -m pytest -p no:cacheprovider tests/video_analysis/test_analyzer.py tests/video_analysis/test_recommendation.py tests/srt tests/projects/test_analysis_jobs.py tests/cli/test_serve_viewer_project_api.py tests/viewer/test_workflow_portal_static.py tests/viewer/test_project_workspace_static.py tests/integration/test_srt_full_pose_workflow.py tests/packaging/test_wheel_contents.py -q`

Expected: PASS.

- [ ] **Step 6: Run full verification**

Run: `python -m pytest -p no:cacheprovider`

Expected: all tests pass with only established skips.

Run: `python scripts/check_no_project_dependency.py`

Expected: success.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 7: Commit integration and docs**

Run: `git add tests/integration/test_srt_full_pose_workflow.py README.md docs/workflow_routing.md docs/technical/developer-guide.md docs/technical/troubleshooting.md && git commit -m "test: verify SRT candidate workflow end to end"`

- [ ] **Step 8: Restart the feature service and perform browser smoke**

Start the server from this worktree on an unused localhost port with an isolated writable storage root. From the project library, verify new-project conditional SRT progress, full-pose route copy, separate FOV/central-meridian fields, candidate progress, candidate confirmation, and absence of browser console errors. Return the project-library URL to the user.

## Self-review Checklist

- [ ] Every production behavior is preceded by a focused failing test.
- [ ] The plan covers every requirement in the approved design spec.
- [ ] No progress percentage is timer-based or invented.
- [ ] `120°` is only a user/project value or test fixture, never a global default.
- [ ] Complete full-pose SRT is never presented as `SRT + 三维重建`.
- [ ] Candidate confirmation is bound to the current successful job and input fingerprint.
- [ ] CAD/SRT replacement invalidates candidates and confirmation; FOV-only edits do not invalidate CRS.
- [ ] No `srt_sfm_fused` production execution or custom projection string is introduced.
