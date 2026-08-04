# Task 3 report: bounded resource queue and workflow adapters

## Status

DONE

- Branch: `feature/video-project-pipeline`
- Worktree: `D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline`
- Base commit: `2444b29` (`fix: stamp analysis pointer provenance`)

## Implementation

- Added immutable `QueueJob` and `AttemptRecord` records. Every job explicitly
  stores dependencies, exclusivity and idempotency keys, input revision and
  fingerprint, adapter identity, output revision, operation ID, and immutable
  attempt history.
- Added `LocalResourceQueue` with one-slot local defaults for
  `heavy_compute`, `light_compute`, `media_io`, and `control`. Scheduling
  respects priority while retaining durable order, validated-current
  dependencies, resource capacity, and active exclusive keys.
- Added structured `AdapterProgress`. Unknown progress omits `fraction`; queue
  updates persist the adapter stage/message without fabricating a percentage.
- Added safe terminal handling for failed, interrupted, cancelled, and
  stale-input work. Windows execution uses a kill-on-close Job Object where
  available; verified PID/start/fingerprint/token fallback termination and
  POSIX process-group termination cover the remaining cases. Cancellation
  verifies that the complete tree exits and never removes attempt diagnostics.
- Added restart restoration. Durable order is retained; an active attempt is
  adopted only when PID, process start time, command fingerprint, and task
  token are all present and exactly verified. Everything else becomes
  `interrupted` and requires explicit retry.
- Added manifest-free adapter contracts and registry. `sfm_only`,
  `srt_sfm_fused`, and `pure_rotation` wrap the existing CLI commands and
  output locations. Validators return structured `AdapterResult` values and
  never update manifests.
- Preserved the existing `srt_full_pose` contract: it is visible in the
  registry but explicitly `available=False` with an interface-only reason.
  Project preflight skips it before enqueue, and no new full-pose mathematics
  or executable path was invented.
- Added `ProjectService` as the sole jobs-manifest publisher. Batch trajectory
  enqueue creates a physical clip-export dependency followed by the bounded
  trajectory solve, groups partial preflight results, isolates idempotency by
  authoritative half-open interval, manifest/input fingerprints, workflow,
  adapter version, and parameters, and publishes only validated current
  results.
- Changed active inputs produce `stale_input`, retain attempt diagnostics,
  clear output publication fields, and are not reported as algorithm failure.
- Exported the Task 3 public interfaces from `cadscene.projects`.

## Review remediation

- Added a real bounded local executor and subprocess worker. Queue-reserved
  attempts are claimed atomically, adapter commands execute strictly in order,
  PID/start time/command fingerprint/task token/log paths are persisted, and
  only `ProjectService` publishes durable job state.
- Made finish and recovery recompute current authoritative identity from source
  files, manifest revisions, exact integer-PTS intervals, parameters, workflow,
  and adapter version. Caller-provided fingerprints are never authoritative.
- Made project restore merge into the global queue so other projects, live
  controllers, ordering, and shared resource capacity are retained.
- Routed SfM through `resolve_sfm_python()` and derived fused-SRT offsets exactly
  from `source_start_pts * source_time_base`, after validating the authoritative
  clip frame map.
- Reused the existing physical clip-export CLI through an attempt-scoped batch
  export plan and validated the resulting MP4/frame-map artifacts before making
  them available to trajectory dependencies.
- Closed executor lifecycle races: normal completion releases its controller,
  retries use only the new attempt controller, cancellation and executor
  publication are serialized safely, and a process launched before identity
  publication failure is terminated rather than orphaned.

## TDD evidence: RED

Initial queue/service API:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_service_jobs.py
2 collection errors
```

Expected reason: `cadscene.projects.adapters` and the Task 3 queue/service
modules did not exist.

Adapter registry:

```text
pytest -q tests/projects/test_workflow_adapters.py
1 collection error
```

Expected reason: `cadscene.projects.workflow_adapters` did not exist.

Service implementation:

```text
pytest -q tests/projects/test_service_jobs.py
1 collection error
```

Expected reason: `cadscene.projects.service` did not exist.

The first service behavior run then produced `2 failed, 2 passed`: clip export
idempotency omitted clip identity, and the stale-input test correctly exposed
that a solve remains queued until its validated export dependency succeeds.

Restart/idempotency self-review regressions:

```text
pytest -q tests/projects/test_service_jobs.py -k "repeated_enqueue or service_restart"
2 failed, 4 deselected
```

Expected reasons: replay created orphan attempt directories, and service-level
restart restoration was not yet implemented.

Progress/adoption/public-interface self-review regressions:

```text
pytest -q tests/projects/test_queue.py -k "stage_only or incomplete"
2 failed, 10 deselected
pytest -q tests/projects/test_service_jobs.py -k "package_exports"
1 failed, 6 deselected
```

Expected reasons: no queue progress update API, incomplete process identity
could be adopted, and package-root exports were missing.

Relevant-manifest idempotency and existing fusion output contract:

```text
pytest -q tests/projects/test_service_jobs.py -k changed_relevant_manifest
1 failed, 7 deselected
pytest -q tests/projects/test_workflow_adapters.py -k partial_srt
1 failed, 13 deselected
```

Expected reasons: manifest revisions were absent from the key, and the first
adapter draft expected `02_srt_fusion` rather than the existing CLI's actual
`02_fusion` directory.

## GREEN verification

Focused Task 3 tests after implementation and self-review:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py
49 passed in 2.87s
```

All project repository/service tests:

```text
pytest -q tests/projects
105 passed in 3.58s
```

Requested workflow and pure-rotation regression set:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/projects/test_executor.py tests/workflow tests/pure_rotation
170 passed, 1 warning in 8.35s
```

Single full-suite run:

```text
pytest -q
652 passed, 1 skipped, 1 warning in 47.00s
```

Static/syntax/whitespace verification:

```text
python -m pyflakes <Task 3 production and test files>
exit 0
python -m compileall -q cadscene/projects tests/projects
exit 0
git diff --check
exit 0
```

## Self-review

- Confirmed Task 2 repository/model invariants were consumed rather than
  weakened; all existing project tests remain green.
- Confirmed Task 1 integer-PTS half-open clip fields participate in the
  idempotency fingerprint and clip-export dependency.
- Confirmed only `ProjectService` writes job manifest state; adapters are
  filesystem-scoped to attempt directories and return values only.
- Confirmed dependency completion is insufficient without validation against
  the dependency's current input fingerprint.
- Confirmed missing restart identity fields fail closed to `interrupted`.
- Confirmed real Windows parent/descendant cancellation performs no
  attempt-directory cleanup, verifies tree exit, and stale results publish no
  output revision or output map.
- Confirmed the three runnable adapters reference existing CLI module names
  and actual output paths. No workflow mathematical internals changed.

## Concerns

- `srt_full_pose` remains deliberately interface-only because this repository
  has no runnable full-pose algorithm/CLI. It is rejected during preflight,
  not queued and failed later.
- Process adoption requires the hosting runner to supply a verifier for PID,
  start time, command fingerprint, and task token. The safe default adopts
  nothing and marks prior active work interrupted.
- The full suite's one skip is the existing platform-specific test, and the
  warning is the existing third-party `fontTools.misc.py23` deprecation.
- `ruff` is not installed in this environment; `pyflakes`, `compileall`,
  pytest, and `git diff --check` were used instead.
