# Task 3 report: bounded resource queue and workflow adapters

## Status

DONE_WITH_CONCERNS

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
  stale-input work. Cancellation delegates to a full process-tree terminator
  (`taskkill /T /F` on Windows; process-group SIGTERM on POSIX) and never
  removes attempt logs or temporary outputs.
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
34 passed in 0.24s
```

All project repository/service tests:

```text
pytest -q tests/projects
91 passed in 0.77s
```

Requested workflow and pure-rotation regression set:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/workflow tests/pure_rotation
156 passed, 1 warning in 3.34s
```

Single full-suite run:

```text
pytest -q
637 passed, 1 skipped, 1 warning in 37.67s
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
- Confirmed cancellation performs no attempt-directory cleanup and stale
  results publish no output revision or output map.
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
