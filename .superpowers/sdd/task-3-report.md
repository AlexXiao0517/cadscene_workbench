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
  token are all present and exactly verified. A proven-absent attempt becomes
  `interrupted`; an unverifiable live PID remains blocked and retains its
  resource/exclusive reservation until absence or safe cleanup is proven.
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

## Second closure-review remediation

The six Important race, recovery, integrity and export findings against
`49ff70d` were reproduced and fixed:

- Every executing attempt now owns an immutable `worker_claim_token`. Process
  identity, progress, validation, success, failure and controller release all
  require the exact attempt number and claim token. Retry is refused while an
  old claim remains, and an old callback/finally cannot mutate or release a
  later retry.
- All queue reads/transitions/scheduling are serialized by one reentrant lock.
  Service terminal operations acquire locks in the fixed order service
  operation lock -> project -> clips -> jobs -> queue. Cancellation first
  publishes `cancelling`, performs blocking tree termination without repository
  or queue locks, then uses the reserved attempt identity to publish the final
  state. A concurrent clips mutation therefore completes before the terminal
  authoritative fingerprint check and produces `stale_input`, never an old
  publication.
- Fully verified restored processes receive an adopted attempt lease. Scheduler
  polling reaps an exited adopted process, conservatively records
  `interrupted` because no identity-bound completion sidecar exists, releases
  resource/exclusive capacity, and asks `ProjectService` to publish the state.
  A service crash after publishing `cancelling` resumes verified cleanup on
  restore and also ends as `interrupted`, never permanently `cancelling`.
- `process_worker` recomputes SHA256 from canonical normalized `commands.json`
  before starting any child and compares it to `--command-fingerprint`.
- Clip export dependencies are per permanent `clip_id`. Their attempt manifest,
  validator and output map contain only the requested clip. The Stage 8A API
  retains strict full-source partition validation by default and adds an
  explicit subset mode whose sidecar still maps every decoded source frame in
  the requested half-open interval exactly once.
- Windows fallback cancellation repeatedly discovers and terminates descendants
  until two quiescent scans, terminates the parent last, and verifies the known
  tree is gone. Failed/unverifiable termination stays `interrupted`; retry is
  refused while the prior process is not proven gone. Windows Job Objects
  remain the primary executor path.

Second-review RED evidence included:

```text
pytest -q tests/projects/test_queue.py -k "cancel_blocks_retry or cancel_reserves_state"
2 failed (retry was accepted and no worker_claim_token existed)
pytest -q tests/projects/test_executor.py -k tampered_plan
1 failed (tampered child command executed)
pytest -q tests/video_analysis/test_clip_export.py -k subset_frame_map
1 failed (subset mapping API did not exist)
pytest -q tests/projects/test_queue.py -k restore_resumes_verified_cancelling
1 failed (cancelling survived restore and retained the resource slot)
```

Each test was then observed GREEN after the corresponding minimal fix.

## GREEN verification

Focused Task 3 tests after implementation and self-review:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py
49 passed in 2.87s
```

Second-review focused set:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/projects/test_executor.py
61 passed in 2.26s
```

All project repository/service tests:

```text
pytest -q tests/projects
105 passed in 3.58s
```

After second-review remediation:

```text
pytest -q tests/projects
118 passed in 2.77s
```

Requested workflow and pure-rotation regression set:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/projects/test_executor.py tests/workflow tests/pure_rotation
170 passed, 1 warning in 8.35s
```

Expanded second-review related set, including Stage 8A physical export and CLI:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/projects/test_executor.py tests/workflow tests/pure_rotation tests/video_analysis/test_clip_export.py tests/video_analysis/test_cli.py
231 passed, 1 skipped, 1 warning in 6.66s
```

Single full-suite run:

```text
pytest -q
652 passed, 1 skipped, 1 warning in 47.00s
```

Fresh full-suite run after second-review remediation:

```text
pytest -q
666 passed, 1 skipped, 1 warning in 34.03s
```

## Third closure-review remediation

Five additional recovery/publication races against `f6ce331` were reproduced
before production changes and closed:

- Changed-input active attempts are no longer made terminal before their old
  process is gone. Fully verified PIDs receive a restore-cleanup reservation,
  remain `cancelling` (therefore retaining resource and exclusive ownership),
  and become `superseded`/`interrupted` only after identity is rechecked and
  tree termination succeeds. Unverified live PIDs remain blocked; access
  denial is not treated as proof of absence.
- Restore cleanup is now explicitly two-phase. The reserved `cancelling`
  state is published under the service/repository/queue locks, process-tree
  termination happens after every such lock is released, and finalization
  reacquires the attempt reservation with lease/CAS checks before publication.
  Cleanup failure stays nonterminal and blocks retry/capacity release.
- Auto-scheduled jobs are publication-gated when the queue is owned by
  `ProjectService`. A worker cannot claim a scheduled job until its owning jobs
  manifest has been atomically published. The adopted-process reaper publishes
  every affected project, including a different project that receives the
  released resource slot. Failed publication leaves the project gated and the
  next coordination pass resynchronizes it before claim.
- Cancellation no longer removes its process controller while entering
  `cancelling`. If the initial cancelling-manifest publication fails before
  becoming durable, the exact prior job/attempt lease is restored and the same
  controller remains available for a deterministic retry. If cancelling did
  become durable before an exception, cleanup proceeds and publishes the final
  state safely.
- Windows descendant enumeration now treats `AccessDenied` and other psutil
  inspection errors as verification failures instead of an empty tree. The
  existing two-scan quiescence rule remains required for successful fallback
  cancellation.

Third-review RED evidence:

```text
pytest -q tests/projects/test_queue.py -k restore_cleans_verified_changed_input
2 failed (verified PID 123 was never terminated)
pytest -q tests/projects/test_service_jobs.py -k adopted_reap_publishes_cross_project
1 failed (p2 memory was running while its manifest remained queued)
pytest -q tests/projects/test_queue.py -k windows_descendant_access_denied
1 failed (AccessDenied was silently interpreted as no descendants)
pytest -q tests/projects/test_service_jobs.py -k "cancel_publication_failure or restore_cancelling_terminates_outside"
2 failed (cancel stayed stranded; unrelated p2 callback was lock-blocked)
```

Third-review focused GREEN:

```text
pytest -q tests/projects/test_queue.py -k "restore_cleans_verified_changed_input or restore_resumes_verified_cancelling or windows_descendant_access_denied"
4 passed
pytest -q tests/projects/test_service_jobs.py -k "cancel_publication_failure or restore_cancelling_terminates_outside or adopted_reap_publishes_cross_project or cross_project_schedule_is_unclaimable"
4 passed
```

Publication-failure recovery is covered deterministically: after p2 publication
fails, p2 is unclaimable while its manifest remains queued; the next reaper
coordination pass republishes p2, then and only then permits the worker claim.

Fresh verification after third-review remediation:

```text
pytest -q tests/projects/test_queue.py tests/projects/test_service_jobs.py tests/projects/test_executor.py
51 passed in 1.75s
pytest -q tests/projects
128 passed in 2.93s
pytest -q tests/projects/test_queue.py tests/projects/test_workflow_adapters.py tests/projects/test_service_jobs.py tests/projects/test_executor.py tests/workflow tests/pure_rotation tests/video_analysis/test_clip_export.py tests/video_analysis/test_cli.py
240 passed, 1 skipped, 1 warning in 6.87s
pytest -q
676 passed, 1 skipped, 1 warning in 33.93s
```

Static/syntax/whitespace verification:

```text
python -m pyflakes <Task 3 production and test files>
exit 0
python -m compileall -q cadscene/projects tests/projects
exit 0
git diff --check
exit 0
python -m black --check <all changed Python files>
11 files would be left unchanged
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
- Confirmed `cancelling` holds resource and exclusive capacity until termination
  is complete, while the blocking terminator itself holds no repository or
  queue lock.
- Confirmed stale old progress, finish and finally/controller-release paths are
  rejected by the attempt lease and cannot release a new retry.
- Confirmed per-clip physical export produces a complete decoded-frame sidecar
  for the requested interval without claiming that it covers the whole source.
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
