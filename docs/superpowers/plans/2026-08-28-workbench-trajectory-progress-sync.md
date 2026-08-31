# Workbench Trajectory Progress Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project management and workbench display the same active trajectory job progress, while returning cancelled trajectory clips to a retryable pending presentation state.

**Architecture:** The project snapshot remains the authoritative source for trajectory `job_id`, `status`, `stage`, and `progress`. A `workflow_start` workbench session discovers and watches an already-active trajectory job instead of presenting a second idle state; runtime data only fills the log panel. Cancelled jobs remain durable for audit, while the project UI presents their clips as pending and batch enqueue explicitly retries the existing idempotent job with a new attempt.

**Tech Stack:** Python 3, dataclass-based project service and queue, vanilla JavaScript, pytest static UI contracts and service integration tests.

## Global Constraints

- Do not change SfM, Pure Rotation, scene-bridge, or trajectory progress algorithms.
- Do not restart the running service while clip-0006 is active.
- Preserve cancelled job history, attempts, logs, and idempotency identity.
- Do not automatically rerun a cancelled job until the user submits it again.
- Only cancelled trajectory jobs are presented as pending; failures retain failure state.

---

### Task 1: Retry cancelled trajectory jobs on the next batch submission

**Files:**
- Modify: `cadscene/projects/service.py` in `_enqueue_trajectory_jobs_locked`
- Test: `tests/projects/test_service_jobs.py`

**Interfaces:**
- Consumes: `LocalResourceQueue.submit(job) -> QueueJob`, `LocalResourceQueue.retry(job_id, AttemptRecord) -> QueueJob`
- Produces: batch trajectory enqueue returns the existing idempotent job ID in active state with an appended immutable attempt.

- [ ] **Step 1: Write the failing service test**

```python
def test_batch_enqueue_retries_cancelled_trajectory_as_a_new_attempt(tmp_path: Path) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    first = service.enqueue_trajectory_jobs("p1")
    trajectory_job_id = first.job_ids[0]
    service.cancel_job("p1", trajectory_job_id)

    repeated = service.enqueue_trajectory_jobs("p1")

    retried = queue.get(trajectory_job_id)
    assert repeated.job_ids == (trajectory_job_id,)
    assert retried.status in {"queued", "preparing", "running"}
    assert [attempt.number for attempt in retried.attempts] == [1, 2]
    assert len(repositories.jobs.load("p1").jobs) == 2
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_service_jobs.py::test_batch_enqueue_retries_cancelled_trajectory_as_a_new_attempt -q`

Expected: FAIL because the second enqueue reuses the cancelled terminal job without appending attempt 2.

- [ ] **Step 3: Add the minimal retry during batch enqueue**

After `submitted_solve = self.queue.submit(solve)`, when `submitted_solve.status == "cancelled"`, create the next immutable attempt directory, call `self.queue.retry`, and use the retried object for `trajectory_ids`. Keep the existing final `_publish_queue_locked(project_id)` as the only publication for the batch.

```python
if submitted_solve.status == "cancelled":
    number = len(submitted_solve.attempts) + 1
    directory = self._attempt_directory(project_id, submitted_solve.job_id, number)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        submitted_solve = self.queue.retry(
            submitted_solve.job_id,
            AttemptRecord(number=number, directory=str(directory)),
        )
    except Exception:
        directory.rmdir()
        raise
```

- [ ] **Step 4: Run focused service tests and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_service_jobs.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add cadscene/projects/service.py tests/projects/test_service_jobs.py
git commit -m "fix: requeue cancelled trajectory batches"
```

### Task 2: Present cancelled trajectory clips as pending

**Files:**
- Modify: `apps/project_workspace/project_workspace.js` in row rendering and summary counts
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: raw snapshot clip with `status == "cancelled"`, `job_id`, and `capabilities.can_retry`
- Produces: `trajectoryDisplayStatus(clip) -> string`, used consistently by the row status pill and pending count.

- [ ] **Step 1: Write the failing static UI contract**

```python
def test_cancelled_trajectory_returns_to_pending_presentation() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    assert "function trajectoryDisplayStatus(clip)" in script
    assert 'clip.status === "cancelled" ? "ready" : clip.status' in script
    assert "trajectoryDisplayStatus(clip)" in script
    assert "snapshot.clips.filter((clip) => trajectoryDisplayStatus(clip) === \"ready\")" in script
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py::test_cancelled_trajectory_returns_to_pending_presentation -q`

Expected: FAIL because cancelled clips display “已取消” and are omitted from the pending count.

- [ ] **Step 3: Implement one presentation helper**

```javascript
function trajectoryDisplayStatus(clip) {
  return clip.status === "cancelled" ? "ready" : clip.status;
}
```

Use it for `.status-pill` and count only `trajectoryDisplayStatus(clip) === "ready"` as pending. Keep `clip.status` unchanged for API actions, retry eligibility, and audit behavior.

- [ ] **Step 4: Run the project workspace UI tests and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add apps/project_workspace/project_workspace.js tests/viewer/test_project_workspace_static.py
git commit -m "fix: return cancelled clips to pending view"
```

### Task 3: Auto-bind active project trajectory jobs in the workbench

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js` in project workbench bootstrap and trajectory watcher
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: project snapshot clip with an active trajectory `job_id`, `status`, `stage`, and `progress`
- Produces: `attachActiveProjectWorkbenchTrajectory() -> Promise<boolean>` and snapshot-driven `renderProjectTrajectorySnapshot(clip)`; existing `waitForProjectWorkbenchTrajectory(job_id)` remains the terminal-state loop.

- [ ] **Step 1: Write failing workbench contracts**

```python
def test_workflow_start_session_auto_binds_an_active_project_trajectory() -> None:
    script = _read("workflow.js")
    assert "async function attachActiveProjectWorkbenchTrajectory" in script
    assert 'activeStatuses.has(clip.status)' in script
    assert "projectWorkbenchTrajectoryJobId = clip.job_id" in script
    assert "waitForProjectWorkbenchTrajectory(clip.job_id)" in script


def test_project_trajectory_progress_uses_snapshot_and_runtime_only_updates_log() -> None:
    script = _read("workflow.js")
    wait = script[
        script.index("async function waitForProjectWorkbenchTrajectory") :
        script.index("async function runProjectWorkbenchTrajectory")
    ]
    assert "renderProjectTrajectorySnapshot(clip)" in wait
    assert "await renderStatus(runtime.workflow_status)" not in wait
    assert 'runtime.lines.join("\\n")' in wait
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -k "auto_binds_an_active_project_trajectory or progress_uses_snapshot" -q`

Expected: FAIL because a `workflow_start` session displays idle state until the user starts again, and runtime workflow status currently owns the workbench progress.

- [ ] **Step 3: Add snapshot rendering and automatic attachment**

Implement:

```javascript
function renderProjectTrajectorySnapshot(clip) {
  const fraction = clip.progress?.fraction;
  if (typeof fraction === "number") {
    progress.value = Math.max(0, Math.min(1, fraction));
  }
  stateLabel.textContent = projectTrajectoryStatusCopy(clip.status, clip.stage);
  message.textContent = clip.progress?.message || projectTrajectoryStatusCopy(clip.status, clip.stage);
}
```

On `workflow_start` bootstrap, fetch the project snapshot. When the current clip has `queued`, `preparing`, `running`, or `validating` status and a job ID, set the project trajectory ownership fields, show the cancel button, and start `waitForProjectWorkbenchTrajectory` without submitting or retrying anything. In the watcher, always call `renderProjectTrajectorySnapshot(clip)` and use runtime only to update `#workflowLogContent`.

When the watcher observes `cancelled`, clear ownership, hide cancel, restore job actions, show “任务已取消，片段已返回待处理”, and leave the session in `workflow_start` so the user may run again.

- [ ] **Step 4: Run workbench UI tests and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add apps/web_camera_viewer/workflow.js tests/viewer/test_workflow_ui_static.py
git commit -m "fix: synchronize workbench trajectory progress"
```

### Task 4: Integrated verification

**Files:**
- Verify only; no new production files.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: regression evidence and a clean branch.

- [ ] **Step 1: Run focused suites**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_service_jobs.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py -q`

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -p no:cacheprovider -q`

Expected: PASS with no failures.

- [ ] **Step 3: Run repository checks**

Run: `git diff --check`

Expected: no output and exit code 0.

- [ ] **Step 4: Confirm service and task continuity**

Read the live project snapshot and verify clip-0006 was not cancelled or restarted by this work. Do not restart the service while it remains active.
