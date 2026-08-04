# Task 4 Report — Validated upload, project API, and workspace

## Scope and baseline

- Worktree: `D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline`
- Branch: `feature/video-project-pipeline`
- Task baseline: `55758a4`
- Existing workflow mathematics were not modified.

## TDD evidence

1. Upload RED: `tests/projects/test_uploads.py` failed collection because
   `cadscene.projects.uploads` did not exist.
2. Upload GREEN: interrupted/truncated/corrupt uploads, atomic publication,
   fingerprinting, callback order, invalid DXF, and failed replacement history
   now pass.
3. API/UI RED: project API failed collection because
   `cadscene.projects.http_api` did not exist; workspace static assets were
   absent.
4. HTTP dispatch RED: a real PATCH request returned the expected `501` before
   `serve_viewer` project dispatch existed.
5. API/UI/dispatch GREEN: snapshot, revision conflict, capability, preflight,
   thin HTTP dispatch, safe polling, and workspace static contracts pass.
6. Analysis RED: `cadscene.projects.analysis` was missing.
7. Analysis GREEN: bounded automatic analysis, initial activation, immutable
   candidate reanalysis, and preservation of user layers pass.
8. Stale CAD RED/GREEN: a deliberately changed request initially attached the
   completed old CAD import to the replacement asset. The coordinator now
   rechecks the request key under the project lock before attaching results and
   stops before video analysis when the CAD input was superseded.
9. Formal-review RED/GREEN: API/upload/repository escape tests now reject dot
   and relative project IDs through one fail-closed validator.
10. Runtime RED/GREEN: `serve_viewer` now exclusively leases the project root,
    reconciles and restores durable jobs before startup, marks unverifiable old
    running jobs interrupted, and drives the real `LocalJobExecutor` worker.
11. Decode RED/GREEN: full decode now uses `-xerror` and rejects error-level
    stderr even if FFmpeg reports return code zero.
12. Interaction RED/GREEN: confirmation-only preflight, selectable per-clip
    reasons, and real reanalyze/retry/cancel API plus UI events are covered.
13. Snapshot/edit RED/GREEN: the four manifests are read under fixed lock order
    and nullable dirty names remain distinct from absent local edits.

## Implemented behavior

### Validated uploads

- Project-local temporary files and incremental SHA-256.
- Exact expected-size and optional expected-fingerprint verification.
- Video full decode plus authoritative decoded-frame probe.
- SRT parse/coverage validation.
- JSON/ZIP/DWG checks and real DXF parsing before publication.
- Content-addressed destination names and same-directory `os.replace`.
- fsync for files and directories where supported.
- Canonical published validation report plus immutable attempt reports.
- Abort/truncation/corruption never becomes analyzable.
- A failed replacement preserves the previous validated publication.
- Analysis callback runs only after both media and the published report exist.

### Automatic analysis

- The required first-version input set is published video plus CAD; optional
  SRT is included when supplied before CAD.
- A stable input request key is derived from video/CAD/SRT SHA-256 values and
  atomically marked queued in the project manifest.
- The same input key does not trigger analysis twice.
- One local light-analysis worker wraps existing `import_cad` and Stage 8A
  `analyze_video` behavior.
- The first successful video analysis atomically publishes project and clips
  state. Later analysis creates a candidate revision and does not replace the
  active clip/user layers.
- Generated names use `场景 NN · 第 N 段`; custom names and workflow overrides
  remain independent.

### Project API and HTTP entry

- `ProjectApi` owns project route parsing, stable-ID validation,
  `expected_revision` checks, permission scope, capability calculation, and
  service calls.
- `RangeRequestHandler` only parses HTTP JSON/multipart transport and serializes
  `ApiResponse`; it never reads or writes a project manifest.
- Unified snapshot revision hashes all four component revisions.
- The snapshot holds the fixed four-manifest lock order, computes capability
  preflight once, and derives its ETag from that exact state.
- Stable quoted ETag; matching `If-None-Match` returns 304 with a strict empty
  body.
- Snapshot provides server-computed project and clip capabilities.
- `srt_full_pose` remains visible but reports interface-only/unavailable.
- Nullable workflow override restores the recommendation. Workflow changes
  stale old trajectory/workbench/render references without deleting them.
- Nullable custom display name restores the generated name.
- Batch preflight returns eligible/needs-confirmation/skipped per clip and can
  enqueue a partially accepted set.
- Reanalysis, job retry, and job cancellation use expected-revision project
  APIs; job capabilities are server-owned.
- Project IDs share one fail-closed validator across HTTP, uploads, and
  repository path construction.

### Single-machine runtime

- `serve_viewer` exclusively leases the project root; a second instance cannot
  own the same manifests.
- Startup reconciles manifests and restores persisted jobs before worker
  polling. Unverifiable old running jobs become `interrupted` and are not
  adopted.
- The real bounded `LocalJobExecutor` consumes the restored queue and reaps on
  idle polls. Shutdown stops and joins the worker before releasing ownership.

### Project workspace

- Dark, responsive project workspace with exactly 项目概览、项目文件、片段管理、
  设置 in a collapsible icon-only sidebar.
- Product-facing scene/segment name, friendly time range and duration.
- Recommendation uses normal body text; current state and progress are separate.
- Optional measured fraction is shown; stage-only progress is used otherwise.
- Server capabilities exclusively control action availability.
- 1.5-second ETag polling preserves local workflow/name edits and selection.
- A pending `custom_display_name: null` continues to show the generated name
  during polling instead of reviving an older server custom name.
- Workflow reset, inline rename, batch preflight/confirm, accessible live status,
  focus treatment, reduced-motion handling, and one merge action.
- Upload portal uses the new project endpoints in video → optional SRT → CAD
  order and immediately redirects to the project workspace. Existing legacy
  workflow endpoints remain served for old direct clients.
- Each new or retried submission resets the client project revision and stale
  manifest state before project creation.

## Verification

- Formal-review focused set: `45 passed`.
- All project tests: `169 passed`.
- Related project/video-analysis/workflow/pure-rotation set: `500 passed, 1 skipped`.
- Full suite: `739 passed, 1 skipped`.
- `compileall`: pass.
- `pyflakes` on every Python file changed by Task 4 and this review closure:
  pass with no warnings.
- `git diff --check`: pass.
- Only warning: existing third-party `fontTools.misc.py23` deprecation.

The first in-app browser launch was blocked by Windows sandbox `EPERM` while
resolving the user's AppData path. A subsequent real-browser QA run succeeded:

- 1680 x 945 layout rendered correctly.
- Sidebar collapsed from 228 px to 72 px and retained icon-only navigation.
- A 1.8-second ETag poll preserved the selected row and collapsed state.
- At 1265 px there was no document-level horizontal overflow; the table kept
  its own scrolling region.
- No console warning or error was observed.

### FFmpeg environment incident and runtime resolution

One intermediate full-suite run on the then-current default `PATH` selected a
WinGet LGPL FFmpeg build. It produced five environment/toolchain failures: four
clip-export tests failed because that binary has no `libx264`, and one VFR PTS
test differed (`source_end` 1.0 versus 0.9) between FFmpeg builds. Running those
five tests with the project-installed imageio-ffmpeg binary was green.

This is fixed in application code rather than hidden in the test environment:
`resolve_ffmpeg_executable()` now checks whether a discovered system FFmpeg
advertises `libx264` and otherwise falls back to the project-installed
imageio-ffmpeg binary. On the actual default environment it resolves to
`D:\anaconda3\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe`.
The five affected tests then passed directly under the default environment,
and the fresh focused/project/related/full results above were all collected
without an FFmpeg environment override.

Local visual smoke command:

```powershell
cd D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline
python -m cadscene.cli.serve_viewer --root D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline --storage-root D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline --bind 127.0.0.1 --port 8318
```

Open:

```text
http://127.0.0.1:8318/apps/project_workspace/?projectId=<project_id>
```

## Deferred by approved stage order

- Workbench session binding/save/return is Task 5.
- Frame-exact project render, fallback, normalization, and concat are Task 6.
- Their buttons are present only through server capabilities and remain disabled
  until those services exist.

## Second formal review closure (base `059489e`)

This section supersedes the earlier automatic-analysis implementation notes.
The production server no longer creates an in-memory analysis executor or owns
analysis through a `Future`.

### Review finding map: 2 Critical + 4 Important + 1 Minor

1. **Critical — non-durable automatic analysis:** fixed by persisting an
   explicit `cad_analysis -> video_analysis` DAG in `jobs_manifest.json`.
   Both tasks use `light_compute`, explicit dependency IDs, exclusive keys,
   idempotency/input identities, adapter version, immutable attempt directories,
   and the normal `LocalJobExecutor`. `serve_viewer` now directly enqueues this
   DAG through `ProjectService`; restart restoration uses the same queue path.
2. **Critical — superseded CAD polluted formal data:** fixed by running
   `import_cad` only under the CAD job attempt directory. The video completion
   path rechecks the authoritative request fingerprint, validates the successful
   CAD dependency and both output paths against their owning attempt roots, then
   lets `ProjectService` publish immutable formal CAD and video-analysis trees.
   Superseded/stale input never activates or creates a candidate revision.
3. **Important — failed upload changed the canonical pointer:** fixed by
   publishing media, then the immutable published-attempt report, and only then
   atomically switching the canonical validation-report pointer. Injected report
   failure preserves the preceding canonical publication.
4. **Important — empty manifest restore retained stale jobs:** fixed by making
   `merge_restored` require the target `project_id`. An empty authoritative
   manifest clears only that project's in-memory jobs; foreign jobs are rejected
   and other projects remain intact.
5. **Important — Windows project-directory aliases:** fixed by the shared
   project-ID validator rejecting trailing dots/spaces and Windows device names
   (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`), including extensions and
   case variants.
6. **Important — cancelling advertised as cancellable:** fixed by removing
   `cancelling` from the server-provided `can_cancel` capability.
7. **Minor — case-sensitive conditional request header:** fixed by normalizing
   inbound API header names and honoring lowercase or mixed-case
   `If-None-Match`; a matching ETag returns HTTP 304 with no body.

### Additional publication and recovery hardening

- Analysis-busy gating is enforced in `ProjectService` preflight/enqueue, not
  only in UI capabilities, so direct API/service calls cannot launch trajectory
  jobs against old active clips while reanalysis is queued or running.
- CAD manifests are structurally rebased to a canonical immutable dataset ID;
  `dataset`, `data/<old>/...` references, and referenced design files are
  validated through a real `load_dataset_manifest` round trip after publication.
- Existing formal directories left by a crash are reused only after identity,
  structure, referenced-file, and content validation. A retry no longer fails
  permanently on a valid orphan, while conflicting content still fails closed.
- Adapter result paths and the successful CAD dependency path are resolved and
  required to remain beneath the exact immutable job attempt directory.
- Real subprocess failure, retry, cancellation, supersession, structured stage
  updates, and restart interruption synchronize `_analysis.status/error`; the UI
  can reanalyze after terminal interruption instead of remaining stuck.
- The compatibility `ProjectAnalysisCoordinator` is stateless and queue-backed;
  production wiring does not instantiate it.

### TDD and verification evidence for this closure

- Initial review RED: `4 failed, 17 passed` (missing durable analysis enqueue and
  service/UI analysis-busy gate).
- Durable analysis/API/project/runtime focused suite: `58 passed`.
- Upload, queue-restore, stable-ID, ETag and capability regressions are included
  in the full suite.
- Pre-final full suite after the 2C+4I+1M fixes: `763 passed, 1 skipped`.
- Final focused suite after conflicting-orphan coverage: `159 passed`.
- Final fresh full suite: `764 passed, 1 skipped` in 38.63 seconds.
- `python -m compileall -q cadscene`: pass.
- `python -m pyflakes` over every changed Python file: pass with no output.
- `git diff --check`: pass.
- Only observed warning: the existing third-party `fontTools.misc.py23`
  deprecation warning.

## Fourth formal review closure (base `81a12ac`)

### Review finding map: 1 Critical + 4 Important

1. **Critical - analysis completion recovery released the publication lock:**
   `finish_job` now holds the same reentrant `_publication_lock` across the
   complete `_finish_job_once -> reconcile -> durable reload/queue commit`
   sequence. Repository and queue locks remain nested in the established order.
   A concurrent upload/reanalysis cannot replace the analysis request intent
   between a failed activation publication and its recovery decision.
2. **Important - a new analysis DAG rewrote historical job provenance:** all
   three DAG publication paths now assign `operation_id` and
   `submission_operation_id` only to newly prepared candidate IDs. Existing
   jobs are serialized byte-for-byte from their queue objects. Tests preserve
   historical completion/submission provenance and assert the corresponding
   durable and in-memory records remain identical.
3. **Important - execution fingerprint and command plan used different state
   snapshots:** `prepare_job_execution` now performs fingerprint validation and
   the full clip-export, CAD/video-analysis, or trajectory plan construction
   under one `_state_guard`. Concurrent upload or workflow-override mutation
   waits until the old job-bound command and validator closure are fully built;
   no helper can re-read a newer manifest after the fingerprint check.
4. **Important - tree hashes admitted path/content concatenation collisions and
   publication-copy TOCTOU:** tree fingerprints now use a versioned binary
   encoding with entry type, path byte length, content byte length, path bytes,
   and content bytes. Directories and symlinks have explicit types. The known
   `zz_a + bc` versus `zz_ab + c` collision is covered. The publisher rehashes
   copied source subtrees, normalized CAD staging, and final analysis staging
   immediately before atomic publication; any mismatch fails closed.
5. **Important - validators could mutate upload bytes after the stream digest:**
   after validation and before any report or media publication, the temporary
   upload's filesystem size and SHA-256 are recomputed and required to equal the
   streamed size/digest (and therefore any supplied expected hash). A validator
   that replaces bytes with different same-length content now raises
   `UploadValidationError` and publishes no content-addressed media.

### Fourth-round TDD and verification evidence

- Initial focused RED: `5 failed` covering historical DAG provenance, the
  concrete tree-hash collision, unlocked activation recovery, unlocked plan
  construction, and same-length validator mutation.
- Focused GREEN: all five review reproductions pass; an additional final-staging
  copy-mutation test also passes and proves the atomic target is never published.
- All project tests: `206 passed`.
- Related project API + video-analysis + workflow + pure-rotation suite:
  `255 passed, 1 skipped`.
- Final fresh full suite: `783 passed, 1 skipped` in 44.34 seconds.
- `python -m pyflakes` over every changed implementation and test file: pass
  with no output.
- `git diff --check`: pass.
- Only observed warning: the existing third-party `fontTools.misc.py23`
  deprecation warning.

## Third formal review closure (base `28bb393`)

### Review finding map: 4 Critical + 4 Important + 1 Minor

1. **Critical - analysis activation had a crash window:** queue completion is
   now prepared without mutating process-local state. The successful video job,
   project activation/candidate state, optional initial clips manifest, and jobs
   manifest are published with one operation ID. Only the recovered durable job
   is then committed to the queue. A manifest-prefix crash is reconciled in the
   same request and again remains recoverable after restart.
2. **Critical - restart could regress a completed project to running:** restore
   now preserves a validated successful video-analysis job as
   `_analysis.status=success` and project `ready` (or candidate-ready). Failed,
   interrupted, cancelled, stale-input, and superseded analysis jobs map to
   explicit terminal project states instead of falling back to `analyzing`.
3. **Critical - lock inversion and non-atomic enqueue:** HTTP no longer holds
   repository locks while calling `ProjectService`. Upload registration, manual
   reanalysis, and the compatibility enqueue path use queue prepare candidates,
   one project/jobs publication, then queue commit in publication -> repository
   -> queue order. Prefix crashes reconcile immediately; a wholly failed enqueue
   becomes `analysis_failed` and cannot leave queued intent without durable jobs.
4. **Critical - upload publication mixed authority with a mutable canonical
   pointer:** upload completion first publishes full-SHA-256-addressed media and
   an immutable attempt report. `ProjectService` then atomically owns project
   asset and optional DAG state. The canonical validation report is written last
   as a best-effort, repairable diagnostic cache; project manifests reference
   only the immutable asset/report and remain authoritative if that cache write
   fails.
5. **Important - active clips read mutable project assets:** every published
   analysis revision and clip now carries an immutable input snapshot containing
   video/CAD/SRT path and SHA-256 plus CAD dataset and analysis artifact IDs and
   paths. Export, trajectory, SRT input, and input fingerprint resolution read
   the clip snapshot. A newly uploaded candidate video cannot change an active
   clip's command or revive an old result as current.
6. **Important - storage identity followed revisions or trusted claimed
   fingerprints:** CAD datasets and video-analysis artifacts use validated tree
   SHA-256 identities, independent of analysis revision. The publisher recomputes
   both source hashes before naming and recomputes copied staging hashes before
   publication, closing validate-to-publish substitution. Existing targets are
   reused only for identical content; conflicting content fails closed. Upload
   paths likewise use the complete SHA-256 and never overwrite different bytes.
7. **Important - worker, adapter, validation, and publication concerns were
   coupled in `service.py`:** lightweight command construction and adapter output
   validation now live in `analysis_adapters.py`; immutable formal publication
   and CAD manifest rebasing live in `analysis_publication.py`. The worker remains
   CLI-only and adapters return structured results; only `ProjectService` changes
   manifests.
8. **Important - candidate analysis lacked durable activation inputs:** project
   `_analysis_revisions[revision]` now records its immutable input snapshot,
   content-addressed artifact descriptor, and validated clip manifest location.
   Candidate completion does not change the active clips manifest, but a later
   explicit activation can reconstruct the candidate from its immutable artifact.
9. **Minor - operation provenance was ambiguous and duplicate legacy helpers
   remained:** queue jobs now retain `submission_operation_id` separately from
   `publication_operation_id`; completion does not erase DAG submission
   provenance. The duplicate legacy publisher/validator helpers and unused HTTP
   analysis callback were removed, and successful activation tests assert the
   shared completion publication operation explicitly.

### Third-round implementation details

- Queue submission and success completion both use explicit prepare/commit
  candidates. Prepared candidates are invisible to workers until the owning
  manifest publication succeeds or is reconciled from a durable prefix.
- Cross-manifest analysis completion records one operation ID on project,
  initial clips when applicable, and the successful jobs entry. Retry attempts
  remain immutable and old input/output revisions remain isolated.
- Upload storage is two-phase: immutable content/report first, service-owned
  manifest registration second, canonical diagnostic pointer last. No later
  runtime read depends on the canonical pointer.
- Clip-bound source lookup is backward compatible: newly analyzed clips use the
  immutable snapshot, while legacy clips retain their prior manifest-revision
  input binding. User-owned clip parameters and workflow resolution continue to
  invalidate affected jobs without candidate project uploads invalidating active
  clips.
- The content-addressed candidate descriptor contains the artifact ID/path and
  input snapshot needed for future explicit activation without consulting mutable
  current project assets.

### Final TDD and verification evidence

- Queue two-phase RED/GREEN tests cover invisible success candidates and
  submission candidates that do not mutate the scheduler before publication.
- Fault-injection tests cover analysis-completion jobs-manifest prefix recovery,
  restart after recovered activation, manual-reanalysis prefix recovery, the
  second upload's automatic DAG prefix recovery, total enqueue failure, and a
  canonical-cache write failure after authoritative upload registration.
- Snapshot isolation tests prove a replacement/candidate video does not alter
  active clip export input and prove the candidate artifact descriptor is
  independently readable.
- Content tests use real tree hashes and reject forged adapter fingerprints,
  copied-tree mismatches, conflicting immutable artifacts, and conflicting
  full-SHA-256 upload targets.
- Final project + project-API suite: `224 passed`.
- Final fresh full suite: `778 passed, 1 skipped` in 39.25 seconds.
- `python -m compileall -q cadscene tests`: pass.
- `python -m pyflakes` over every changed Python and test file: pass with no
  output.
- `git diff --check`: pass.
- Only observed warning: the existing third-party `fontTools.misc.py23`
  deprecation warning.
