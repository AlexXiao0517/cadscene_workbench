# Workbench Resume State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reopen a project clip at its last durable workflow stage and source PTS while preserving every saved manual keyframe and warning about unsaved camera edits.

**Architecture:** Add a project-local `workbench_resume/<clip_id>.json` record with its own optimistic revision and atomic writer. Workbench sessions authorize updates to that record, while business artifacts remain authoritative. The viewer loads the fitted trajectory as the continuous baseline, overlays the latest manual anchors by frame, and restores stage/PTS only after validating the current session and decoded-frame timeline.

**Tech Stack:** Python 3.10 dataclasses and atomic JSON files, existing ProjectService/ProjectApi, vanilla browser JavaScript, Node-backed JavaScript unit tests, pytest.

## Global Constraints

- Do not store resume state in clips/jobs manifests.
- Do not auto-save an unconfirmed camera pose as a keyframe.
- Store playback position as source decoded-frame integer PTS plus exact time base; never use `currentTime × fps` as authority.
- Existing manual, fitted, quality, render, annotation, and queue artifacts remain authoritative.
- A missing, corrupt, stale, or unsupported resume record must not block workbench entry.
- Old projects without resume state keep their current fallback behavior.
- Browser close warnings use the standard `beforeunload` mechanism and therefore browser-controlled copy.
- Keep the current branch isolated from `main` and do not rebuild the offline bundle until source tests and a live smoke pass.

---

### Task 1: Project-local resume state domain and atomic store

**Files:**
- Create: `cadscene/projects/workbench_resume.py`
- Test: `tests/projects/test_workbench_resume.py`

**Interfaces:**
- Produces: `WorkbenchResumeState`, `AtomicWorkbenchResumeStore.load_optional(project_id, clip_id)`, and the explicitly typed `AtomicWorkbenchResumeStore.update` method shown below.
- Consumes: `validate_project_id`, `RevisionConflict`, UTC timestamps, and project-local filesystem storage.

- [ ] **Step 1: Write failing store tests**

Add tests proving a missing record returns `None`, the first update creates revision 0, subsequent updates require `expected_revision`, writes survive a new store instance, and corrupt/schema-invalid records are ignored by `load_optional`.

```python
def test_resume_store_survives_reconstruction_and_checks_revision(tmp_path: Path) -> None:
    first = AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock)
    created = first.update(
        "project-1",
        "clip-1",
        expected_revision=None,
        operation_id="resume-op-1",
        workflow_stage="keyframes",
        source_pts=104,
        source_time_base={"numerator": 1, "denominator": 1000},
        trajectory_output_revision="trajectory-1",
        workbench_output_revision=None,
        quality_revision=None,
        render_revision=None,
    )
    assert created.revision == 0
    assert AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock).load_optional(
        "project-1", "clip-1"
    ) == created

    with pytest.raises(RevisionConflict):
        first.update(
            "project-1",
            "clip-1",
            expected_revision=9,
            operation_id="resume-op-2",
            workflow_stage="quality",
            source_pts=111,
            source_time_base={"numerator": 1, "denominator": 1000},
            trajectory_output_revision="trajectory-1",
            workbench_output_revision=None,
            quality_revision="quality-1",
            render_revision=None,
        )
```

- [ ] **Step 2: Run the store tests and verify RED**

Run:

```powershell
python -m pytest -p no:cacheprovider tests/projects/test_workbench_resume.py -q
```

Expected: collection fails because `cadscene.projects.workbench_resume` does not exist.

- [ ] **Step 3: Implement the minimal state and store**

Implement a frozen dataclass with strict validation and a per-record atomic writer:

```python
@dataclass(frozen=True)
class WorkbenchResumeState:
    schema_version: str
    revision: int
    operation_id: str
    updated_at: str
    project_id: str
    clip_id: str
    workflow_stage: str
    source_pts: int
    source_time_base: dict[str, int]
    trajectory_output_revision: str | None
    workbench_output_revision: str | None
    quality_revision: str | None
    render_revision: str | None

class AtomicWorkbenchResumeStore:
    def load_optional(self, project_id: str, clip_id: str) -> WorkbenchResumeState | None:
        path = self.path_for(project_id, clip_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            state = WorkbenchResumeState.from_dict(payload)
            self._validate_identity(project_id, clip_id, state)
            return state
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def update(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int | None,
        operation_id: str,
        workflow_stage: str,
        source_pts: int,
        source_time_base: Mapping[str, object],
        trajectory_output_revision: str | None,
        workbench_output_revision: str | None,
        quality_revision: str | None,
        render_revision: str | None,
    ) -> WorkbenchResumeState:
        current = self.load_optional(project_id, clip_id)
        current_revision = None if current is None else current.revision
        if current_revision != expected_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=-1 if expected_revision is None else expected_revision,
                current_revision=-1 if current_revision is None else current_revision,
            )
        state = WorkbenchResumeState(
            schema_version=RESUME_SCHEMA_VERSION,
            revision=0 if current is None else current.revision + 1,
            operation_id=operation_id,
            updated_at=self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            project_id=validate_project_id(project_id),
            clip_id=clip_id,
            workflow_stage=workflow_stage,
            source_pts=source_pts,
            source_time_base=normalize_time_base(source_time_base),
            trajectory_output_revision=trajectory_output_revision,
            workbench_output_revision=workbench_output_revision,
            quality_revision=quality_revision,
            render_revision=render_revision,
        )
        self._validate_identity(project_id, clip_id, state)
        self._atomic_write(self.path_for(project_id, clip_id), state)
        return state
```

`path_for` first requires `is_safe_stable_id(clip_id)` and then returns `projects/<project_id>/workbench_resume/<clip_id>.json`; `WorkbenchResumeState.from_dict` parses every dataclass field; `normalize_time_base` returns `{"numerator": int, "denominator": int}` after requiring positive values; `_validate_identity` accepts only `sfm`, `keyframes`, `quality`, or `render` and rejects bool-as-int PTS. `_atomic_write` serializes `state.to_dict()` through `NamedTemporaryFile`, `fsync`, and `os.replace`.

- [ ] **Step 4: Run the store tests and verify GREEN**

Run the command from Step 2. Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add cadscene/projects/workbench_resume.py tests/projects/test_workbench_resume.py
git commit -m "feat: persist workbench resume state"
```

---

### Task 2: Session-authorized resume API and validated stage selection

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/cli/serve_viewer.py`
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`

**Interfaces:**
- Consumes: `AtomicWorkbenchResumeStore` from Task 1 and the existing session token/binding checks.
- Produces: `POST /api/projects/<project>/workbench-sessions/<token>/resume` and a `resume_state` object in create/inspect/heartbeat payloads.

- [ ] **Step 1: Add failing API tests**

Cover all of these behaviors:

```python
def test_workbench_session_persists_and_returns_resume_state(tmp_path: Path) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "return_to": "/apps/project_workspace/?projectId=project-1",
            "expected_revision": repositories.clips.load("project-1").revision,
        },
    )
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/resume",
        json_body={
            "expected_resume_revision": None,
            "operation_id": "resume-op-1",
            "workflow_stage": "keyframes",
            "source_pts": 104,
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "quality_revision": None,
            "render_revision": None,
        },
    )
    assert saved.status == 200
    assert saved.body["resume_state"]["source_pts"] == 104
    inspected = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{opened.body['token']}"
    )
    assert inspected.body["resume_state"]["workflow_stage"] == "keyframes"
```

Also test that a token for another clip cannot mutate the record, revision conflicts return the existing API conflict contract, corrupt records produce `resume_state: null`, `workflow_start` clamps to `sfm`, a trajectory-ready session accepts `keyframes`, `quality` requires the fitted alignment artifact, and `render` requires a current saved workbench output.

- [ ] **Step 2: Run focused API tests and verify RED**

```powershell
python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -k "resume_state or resume_stage" -q
python -m pytest -p no:cacheprovider tests/cli/test_serve_viewer_project_api.py -k "workbench" -q
```

Expected: route/payload assertions fail because no resume action or store is wired.

- [ ] **Step 3: Wire the store into ProjectWorkbenchService**

Construct `AtomicWorkbenchResumeStore(projects_root, now=self.now)` in `ProjectWorkbenchService`. Add:

```python
def update_resume(
    self,
    project_id: str,
    token: str,
    *,
    expected_resume_revision: int | None,
    operation_id: str,
    workflow_stage: str,
    source_pts: int,
    source_time_base: Mapping[str, object],
    quality_revision: str | None,
    render_revision: str | None,
) -> WorkbenchResumeState:
    session = self.inspect(project_id, token)
    validated_stage = self._validated_resume_stage(session, workflow_stage)
    return self.resume_store.update(
        project_id,
        session.clip_id,
        expected_revision=expected_resume_revision,
        operation_id=operation_id,
        workflow_stage=validated_stage,
        source_pts=source_pts,
        source_time_base=source_time_base,
        trajectory_output_revision=session.trajectory_output_revision or None,
        workbench_output_revision=session.workbench_output_revision,
        quality_revision=quality_revision,
        render_revision=render_revision,
    )
```

`_validated_resume_stage` must use current session binding and run artifacts. It may downgrade but never upgrade: `workflow_start → sfm`; missing `03_alignment/camera_track_pred.json → keyframes`; missing current saved workbench output → at most quality.

- [ ] **Step 4: Expose the HTTP action and payload**

Extend the action regex with `resume`, parse `expected_resume_revision` as `null` or non-negative int, require a non-empty operation id, and return the state dictionary. Change session payload construction so create, inspect, heartbeat, and trajectory-ready responses include:

```json
{
  "resume_state": {
    "revision": 0,
    "workflow_stage": "keyframes",
    "source_pts": 104,
    "source_time_base": {"numerator": 1, "denominator": 1000}
  }
}
```

Update `workbench_url` to prefer the validated resume stage over the old `workbench_output_revision ? render : keyframes` shortcut.

- [ ] **Step 5: Run focused API tests and verify GREEN**

Run the commands from Step 2. Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add cadscene/projects/workbench_sessions.py cadscene/projects/http_api.py cadscene/cli/serve_viewer.py tests/projects/test_workbench_sessions.py tests/cli/test_serve_viewer_project_api.py
git commit -m "feat: expose durable workbench resume state"
```

---

### Task 3: Merge fitted trajectories with the latest manual anchors

**Files:**
- Modify: `apps/web_camera_viewer/keyframe_sources.js`
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Test: `tests/viewer/test_keyframe_sources.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Produces: `CadsceneKeyframes.mergeFittedTrackWithManual(fitted, manual)`.
- Consumes: existing `confirmedManualKeyframes` source classification.

- [ ] **Step 1: Add failing JavaScript behavior tests**

Run the browser helper through Node and assert that manual anchors replace fitted entries at the same frame while predicted frames remain:

```javascript
const api = require(process.argv[1]);
const merged = api.mergeFittedTrackWithManual(
  { fps: 25, keyframes: [
    { frame: 0, source: "algorithm_prediction", camera: { x: 1 } },
    { frame: 10, source: "algorithm_prediction", camera: { x: 2 } },
  ] },
  { fps: 25, keyframes: [
    { frame: 10, source: "manual_anchor", camera: { x: 99 } },
    { frame: 20, source: "manual_anchor", camera: { x: 100 } },
  ] },
);
process.stdout.write(JSON.stringify(merged));
```

Expected frames are `[0, 10, 20]`, frame 10 has `source === "manual_anchor"`, and input objects are not mutated. Add compatibility tests for missing fitted/manual payloads and legacy source-less two-anchor tracks.

- [ ] **Step 2: Run helper tests and verify RED**

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_keyframe_sources.py -q
```

Expected: the Node process reports `mergeFittedTrackWithManual is not a function`.

- [ ] **Step 3: Implement the pure merge helper**

Use confirmed manual anchors only and replace by integer `frame`:

```javascript
function mergeFittedTrackWithManual(fitted, manual) {
  if (!fitted || !Array.isArray(fitted.keyframes)) return manual || fitted;
  if (!manual || !Array.isArray(manual.keyframes)) return fitted;
  const byFrame = new Map(fitted.keyframes.map((item) => [Number(item.frame), { ...item }]));
  for (const anchor of confirmedManualKeyframes(manual.keyframes)) {
    if (Number.isInteger(Number(anchor.frame))) byFrame.set(Number(anchor.frame), { ...anchor });
  }
  return {
    ...fitted,
    keyframes: Array.from(byFrame.values()).sort((a, b) => Number(a.frame) - Number(b.frame)),
  };
}
```

- [ ] **Step 4: Load both fitted and manual tracks in the viewer**

Change `loadTrackFromPaths` so it fetches the first available primary payload, separately fetches the manual fallback when a fitted payload exists, merges them, then calls `applyTrackPayload` once. An explicit `?track=` URL with no fallback retains existing behavior. A failed manual request must leave the fitted trajectory usable; malformed manual JSON must display a restore warning rather than silently pretending it was merged.

- [ ] **Step 5: Run viewer tests and verify GREEN**

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_keyframe_sources.py tests/viewer/test_workflow_ui_static.py -q
node --check apps/web_camera_viewer/keyframe_sources.js
node --check apps/web_camera_viewer/viewer_legacy.js
```

Expected: all tests and syntax checks pass.

- [ ] **Step 6: Commit Task 3**

```powershell
git add apps/web_camera_viewer/keyframe_sources.js apps/web_camera_viewer/viewer_legacy.js tests/viewer/test_keyframe_sources.py tests/viewer/test_workflow_ui_static.py
git commit -m "fix: restore saved manual keyframes on reopen"
```

---

### Task 4: Authoritative PTS and workflow-stage resume in the browser

**Files:**
- Modify: `apps/web_camera_viewer/annotation_pts.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_annotation_pts.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: session `resume_state` from Task 2.
- Produces: `CadsceneAnnotationPts.seekSourcePts(sourcePts)`, `sourceTimeBase()`, and debounced resume writes.

- [ ] **Step 1: Add failing PTS helper tests**

Add Node-backed tests for exact, nearest, first, and last PTS lookup:

```javascript
const result = api.clipTimeAtSourcePts([
  { source_pts: 100, clip_time_sec: 0.0 },
  { source_pts: 104, clip_time_sec: 0.04 },
  { source_pts: 111, clip_time_sec: 0.11 },
], 108);
```

Expected: `{source_pts: 111, clip_time_sec: 0.11}`. Never calculate from FPS.

- [ ] **Step 2: Add failing workflow static tests**

Assert that `workflow.js`:

- restores `resume_state.workflow_stage` through `setWorkflowStage` after session bootstrap;
- waits for the annotation PTS authority before calling `seekSourcePts`;
- posts `source_pts` and `source_time_base` to `/resume` after pause, seek, stage change, and explicit save using a debounce;
- retries one revision conflict after refreshing the current session;
- does not use `sessionStorage` as the authoritative project resume record.

- [ ] **Step 3: Run browser tests and verify RED**

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_annotation_pts.py tests/viewer/test_workflow_ui_static.py -q
```

Expected: helper and resume integration assertions fail because the behavior is absent.

- [ ] **Step 4: Implement PTS lookup and readiness**

Extend the browser authority with:

```javascript
function clipTimeAtSourcePts(frames, sourcePts) {
  if (!Array.isArray(frames) || frames.length === 0) return null;
  const target = Number(sourcePts);
  let low = 0;
  let high = frames.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (Number(frames[middle].source_pts) < target) low = middle + 1;
    else high = middle;
  }
  const after = frames[low];
  const before = low > 0 ? frames[low - 1] : after;
  const selected = Math.abs(Number(before.source_pts) - target)
    <= Math.abs(Number(after.source_pts) - target) ? before : after;
  return {
    source_pts: Number(selected.source_pts),
    clip_time_sec: Number(selected.clip_time_sec),
  };
}

sourceTimeBase() {
  return timeBase ? { ...timeBase } : null;
},

seekSourcePts(sourcePts) {
  const selected = clipTimeAtSourcePts(frames, sourcePts);
  if (!selected || !video) return null;
  video.currentTime = Number(selected.clip_time_sec);
  return Number(selected.source_pts);
}
```

`configure` stores the exact time base from the annotation preview response and dispatches `cadscenePtsAuthorityReady` after frames are available.

- [ ] **Step 5: Implement debounced resume writes and restore**

Maintain `resumeWriteTimer`, `resumeWriteInFlight`, and the latest `resume_state.revision`. `persistWorkbenchResume()` must read `currentSourcePts()` and `sourceTimeBase()`, use a new operation id, and post only navigation state. Invoke it after `setWorkflowStage`, pause, seek, successful keyframe save, quality completion, and before internal navigation. Do not invoke it for each playback `timeupdate`.

On bootstrap, select the validated server stage, wait for `cadscenePtsAuthorityReady`, seek the saved PTS once, and keep project snapshot task progress authoritative.

- [ ] **Step 6: Run browser tests and verify GREEN**

Run the commands from Step 3, followed by:

```powershell
node --check apps/web_camera_viewer/annotation_pts.js
node --check apps/web_camera_viewer/workflow.js
```

Expected: all tests and syntax checks pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add apps/web_camera_viewer/annotation_pts.js apps/web_camera_viewer/workflow.js tests/viewer/test_annotation_pts.py tests/viewer/test_workflow_ui_static.py
git commit -m "feat: resume workbench stage and source pts"
```

---

### Task 5: Warn on unsaved camera drafts without auto-saving them

**Files:**
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Produces: `window.cadsceneHasUnsavedCameraDraft()` and `cadsceneCameraDraftChanged` events.
- Consumes: successful keyframe save/delete outcomes from existing viewer controls.

- [ ] **Step 1: Add failing dirty-state tests**

Assert that camera range/number user input marks a draft dirty, successful `exportTrack()` clears it, programmatic pose playback does not mark it, and `beforeunload` prevents departure only while the draft is dirty.

```python
assert 'window.cadsceneHasUnsavedCameraDraft = () => cameraDraftDirty' in viewer
assert 'window.addEventListener("beforeunload"' in workflow
assert "event.preventDefault()" in before_unload_block
assert "event.returnValue = \"\"" in before_unload_block
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -k "unsaved or beforeunload" -q
```

Expected: assertions fail because no camera draft contract exists.

- [ ] **Step 3: Implement the minimal dirty-state contract**

Set dirty only inside user input handlers. Clear it only after the camera-track save request returns `ok`, after an explicit keyframe delete is saved, or when a loaded track replaces the draft. Expose a read-only global function and dispatch a change event for future UI copy.

Register:

```javascript
window.addEventListener("beforeunload", (event) => {
  if (!window.cadsceneHasUnsavedCameraDraft?.()) return;
  event.preventDefault();
  event.returnValue = "";
});
```

Do not write the draft pose into the resume API or manual track during `pagehide`.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -q
node --check apps/web_camera_viewer/viewer_legacy.js
node --check apps/web_camera_viewer/workflow.js
```

Expected: tests and syntax checks pass.

- [ ] **Step 5: Commit Task 5**

```powershell
git add apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/workflow.js tests/viewer/test_workflow_ui_static.py
git commit -m "fix: warn before discarding camera drafts"
```

---

### Task 6: Regression and real close/reopen smoke

**Files:**
- Modify only if a failing regression test proves an implementation bug.
- Test: `tests/projects/test_workbench_resume.py`
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/viewer/test_keyframe_sources.py`
- Test: `tests/viewer/test_annotation_pts.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces verification evidence and a clean feature branch suitable for later bundle rebuilding.

- [ ] **Step 1: Run the focused suite**

```powershell
python -m pytest -p no:cacheprovider tests/projects/test_workbench_resume.py tests/projects/test_workbench_sessions.py tests/viewer/test_keyframe_sources.py tests/viewer/test_annotation_pts.py tests/viewer/test_workflow_ui_static.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the full suite and repository checks**

```powershell
python -m pytest -p no:cacheprovider -q
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: full regression passes, dependency check exits zero, and `git diff --check` prints nothing.

- [ ] **Step 3: Run a real project smoke without editing user data automatically**

Start the feature worktree service against a writable manual-acceptance copy. In the browser:

1. open an SfM clip with a fitted trajectory;
2. add and save one manual keyframe without re-running alignment;
3. seek to a known source PTS and remain on the keyframe stage;
4. close the workbench page and stop the service;
5. restart the same service with the same storage root;
6. reopen the clip and verify the saved anchor, stage, PTS, and task status;
7. drag the camera without saving and verify the close warning;
8. cancel the warning, save the keyframe, and verify closing no longer warns.

- [ ] **Step 4: Record the final branch state**

```powershell
git status --short --branch
git log -8 --oneline
```

Expected: clean `codex/async-job-progress-scene-positioning`; do not merge `main` or rebuild the tester bundle until the user approves the smoke result.
