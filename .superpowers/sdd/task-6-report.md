# Task 6 Report - standard-media render and concat

## Substage 1: pure media contracts

This substage implements only the in-memory media contract boundary in
`cadscene/projects/media.py`. It does not invoke FFmpeg/FFprobe, create jobs,
write manifests, publish media, or connect service, API, queue, concat, or UI
behavior.

Implemented contracts:

- `ProjectMediaSpec` owns positive display dimensions, baked display
  orientation, exact SAR and video time base, pixel format, codec/profile,
  optional exact nominal frame rate, and explicit color range/space/transfer/
  primaries. It has a JSON-safe exact-rational round trip.
- `parse_ffprobe()` parses an already-supplied ffprobe structure into explicit
  video/audio stream information and presentation-order decoded video PTS. It
  detects non-zero rotation metadata as an unbaked display orientation and
  prefers frame PTS with best-effort fallback only when PTS is absent.
- `validate_video_pts()` requires a non-empty, zero-start, non-negative,
  strictly increasing rendered-video PTS sequence while allowing irregular
  passthrough timing.
- `validate_render_frame_map()` requires schema/time-base validity, contiguous
  output ordinals, integer source decoded-frame ordinals and source PTS, exact
  output-count equality, ordered unique source identities, and exact equality
  with an authoritative `DecodedFrameTimestamp` sequence when publishing.
- `validate_rendered_media()` combines decoded output count/PTS validation with
  the frame-map proof, requires authoritative source frames and exact source
  time base, and rejects unbaked output orientation.
- `media_compatibility()` compares dimensions, baked orientation, SAR, pixel
  format, codec/profile, time base, nominal timing, and all color metadata,
  returning the exact incompatible fields.
- Audio/video validation records measured absolute duration delta and enforces
  `max(0.050, max_source_frame_duration_sec)`.

## TDD evidence

- RED: `tests/projects/test_media.py` failed collection with
  `ModuleNotFoundError: cadscene.projects.media` before production code existed.
- GREEN: `tests/projects/test_media.py` - `23 passed`.
- Related authoritative PTS/export/frame-map regression:
  `tests/projects/test_media.py tests/video_analysis/test_pts.py
  tests/video_analysis/test_clip_export.py tests/video_analysis/test_contracts.py`
  - `77 passed, 1 skipped`.
- `python -m pyflakes cadscene/projects/media.py tests/projects/test_media.py`:
  pass.
- `python -m compileall -q cadscene/projects/media.py`: pass.
- `git diff --check`: pass.

## Deferred Task 6 scope

Render adapters, queue/service state, source-interval fallback, normalization,
concat, original-source audio muxing, immutable media publication, API routes,
and workspace UI remain intentionally unimplemented for later Task 6
substages.

## Media-contract review closure

- `ProjectMediaSpec` now rejects bool/non-integer dimensions, non-positive
  dimensions, non-`Fraction` SAR/time-base/nominal-rate values, and blank or
  whitespace-padded text. Its frozen runtime representation therefore remains
  exact and type-stable.
- Render validation cannot bypass authoritative identity. Callers must provide
  the expected decoded source-frame sequence and exact source time base; map
  ordinals, integer PTS, count, and time base must all match.
- Display rotation parsing consumes both tag rotation and every side-data
  rotation. Conflicting normalized declarations fail closed, and any consistent
  non-zero rotation marks orientation as not baked.
- Audio/video duration comparison uses `Decimal(str(value))`, making the exact
  50 ms boundary inclusive while rejecting any measured excess.
- Integer coercion maps bool, infinity, and `OverflowError` into
  `InvalidMediaContract` instead of leaking implementation exceptions.

Review TDD evidence:

- RED: `18 failed, 24 passed` across strict spec values, rotation conflicts,
  authoritative render identity/time base, decimal boundary, and overflow.
- GREEN: `tests/projects/test_media.py` - `42 passed`.
- Related authoritative PTS/export/frame-map regression -
  `96 passed, 1 skipped`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

### Exact render-prerequisite binding closure

- A trajectory proof now includes the bytes of the named
  `published_outputs["trajectory"]` artifact: its SHA-256 must exactly equal the
  job `output_fingerprint`; a missing, substituted diagnostic, or subsequently
  modified file fails render preflight.
- New Task 5 saved clip references include
  `trajectory_output_fingerprint`. Render preflight requires it to equal the
  selected current trajectory fingerprint. Existing legacy workbench references
  remain readable by the workbench subsystem but fail closed for new renders.
- Render preflight retains the complete `StateReference`, requires the immutable
  workbench manifest `operation_id` to match the reference operation, and binds
  that operation ID into render input/idempotency identity.
- Enqueue repeats the full trajectory/reference/immutable-output verification
  under the project state guard immediately before constructing each job.
- Workbench manifest `source_output_revision` and `source_output_fingerprint`
  intentionally remain the independently validated manual/pure workbench output
  identity; they are not incorrectly equated with trajectory output identity.

Exact-binding TDD evidence:

- RED: trajectory byte tampering, reference trajectory-fingerprint mismatch,
  immutable-manifest operation mismatch, missing Task 5 reference fingerprint,
  and missing render-identity operation provenance each failed before its
  production change.
- Focused GREEN: render jobs `8 passed`; Task 5 saved-reference repair
  `1 passed, 58 deselected`.
- Related render/service/queue/workbench/workflow/media regression:
  `235 passed`.

### Render preflight main-review closure

- `ProjectManifest` now owns the paired `media_spec_revision` and JSON-safe
  `media_spec` value. Older manifests without either field remain readable;
  partial pairs fail model validation.
- `ProjectService.set_project_media_spec()` performs an expected-revision,
  project-locked update. Preflight, enqueue, and current-input validation always
  reload and parse the media spec for the addressed project; there is no
  process-global project media specification.
- Distinct project media specs survive service reconstruction and queue restore
  without cross-project leakage.
- `clip_render` current-input fingerprint resolution reuses the exact enqueue
  identity payload. It revalidates the current clip/workflow and adapter
  name/version, exact persisted trajectory dependency and artifact hash, saved
  workbench binding and immutable operation, and current project media spec.
- A current unstarted render becomes `interrupted` on restart rather than being
  falsely superseded. Workflow, workbench, trajectory, media-spec, adapter
  version, or trajectory-file changes produce `superseded`.
- The authoritative trajectory output must resolve inside the exact current
  immutable attempt directory and its bytes must match the recorded SHA-256;
  an equally hashed file outside that directory is rejected.

Main-review TDD evidence:

- RED: missing ProjectManifest fields, missing service media-spec setter, false
  superseding of a current render on restore, and acceptance of an out-of-attempt
  trajectory artifact each failed before production changes.
- GREEN: model media-spec roundtrip `1 passed`; render tests `17 passed`,
  including one current restore plus six independent invalidation cases.
- Related models/repositories/recovery/render/service/queue/workbench/workflow/
  media regression: `309 passed`.

## Substage 2a: manifest-free render adapter contracts

This narrowed substage adds only the fake-friendly adapter boundary in
`cadscene/projects/render_adapters.py`; it does not modify the project service,
queue, repositories, or manifests and does not implement FFmpeg commands.

- `RenderInputs` validates safe project/clip identity, explicit workflow,
  absolute existing physical-video/frame-map/workbench inputs, an absolute
  attempt directory, a non-empty contiguous authoritative decoded-frame
  sequence, exact positive source time base, immutable workbench output
  identity, and the project media specification.
- `RenderExecutionPlan` contains non-empty command token tuples and wraps its
  validator so only a structured `AdapterResult` can cross the boundary.
- `RenderAdapter` exposes only immutable validated inputs and a structured
  execution plan; repositories and manifests are not present in its contract.
- `RenderAdapterRegistry` routes by workflow, rejects duplicate workflow or
  adapter name/version identities, and fails closed for unknown workflows.
- Adapter progress reuses `AdapterProgress`; an unknown fraction remains
  stage-only and is omitted from serialization.

Substage 2a TDD evidence:

- RED: test collection failed with `ModuleNotFoundError` before
  `cadscene.projects.render_adapters` existed.
- Focused GREEN: `21 passed`.
- Related media/workflow-adapter/executor/queue regression: `143 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

## Substage 2b: render preflight and enqueue DAG

This substage adds only server-owned render preflight and durable queue
submission. It intentionally does not prepare or execute adapters, publish
render results, or write `render_manifest.json`.

- `ProjectService` accepts an optional render registry and a paired injected
  `ProjectMediaSpec` plus immutable media-spec revision; existing construction
  remains valid when rendering is not configured.
- Per-clip preflight requires a supported current resolved workflow, a current
  trajectory with exact success/validation/input proof and real output, and a
  saved workbench reference bound to that workflow, clip input, trajectory job,
  and trajectory output revision.
- The immutable workbench output manifest is revalidated from the project-owned
  revision directory. Its self-fingerprint, artifact SHA-256/size, identity,
  and resolved-path containment must all match before a clip is eligible.
- Preflight returns `eligible`, `confirmation_required`, and `skipped` clip
  groups with per-clip reasons and permits partial enqueue.
- Enqueued jobs use `clip_render`, `media_io`, an explicit trajectory dependency,
  `render:<project>:<clip>` exclusivity, and adapter name/version. Their input
  and idempotency identities bind project/clips revisions, the half-open clip
  interval, workflow, trajectory proof, workbench output identity, project
  media specification/revision, and adapter version.
- Repeated requests reuse the same job. Default `media_io` capacity starts one
  render and leaves the next queued; changing the adapter version creates a new
  queued identity without concurrent execution for the same clip.

Substage 2b TDD evidence:

- RED: the first `3` focused tests failed because `ProjectService` had no render
  dependency injection or render preflight/enqueue APIs. A fourth exact-proof
  test then failed because a diagnostic file could stand in for the required
  published trajectory artifact.
- Focused GREEN: `4 passed`.
- Related render-adapter/service/queue/workbench/workflow/media regression:
  `231 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

Substage 2a minor hardening:

- Adapter parameters are recursively copied into JSON-safe immutable mappings
  and tuples. Non-string keys, non-JSON values, and non-finite numbers fail
  closed, preventing queued input fingerprints from drifting before execution.
- Authoritative adapter frames independently require real non-bool integer
  ordinals and PTS values. Workbench output revisions are normalized text.
- Eight focused cases failed before the hardening; the adapter/media/workflow/
  executor/queue regression now passes (`151 passed`).

## Narrow media-contract review closure

- The publishing-level `validate_rendered_media()` boundary now rejects a
  missing, non-sequence, string-like, empty, or wrongly typed authoritative
  decoded-frame collection before performing output validation.
- The authoritative source time base must be an exact, positive `Fraction`;
  `None`, floating-point values, and non-positive rationals fail closed.
- Render frame-map `schema_version` is parsed through integer validation and
  additionally requires the original JSON value to be a non-bool `int`
  exactly equal to `1`; values such as `true` and `1.0` are rejected.

Narrow-review TDD evidence:

- RED: all `9` focused cases failed before the production change.
- Focused GREEN: `9 passed, 42 deselected`.
- Media plus authoritative PTS/export/frame-map regression:
  `105 passed, 1 skipped`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

Final minor hardening:

- Publishing also requires every authoritative frame `ordinal` and integer PTS
  to be a real non-bool `int`; integral floats and booleans cannot exploit
  Python numeric equality when compared with the frame map.
- The new four-case regression failed before the check and now passes. Media plus
  authoritative PTS/export/frame-map regression: `109 passed, 1 skipped`.

## Substage 2c: clip-render execution and atomic publication

This substage closes only the queued `clip_render` execution lifecycle. It does
not add real FFmpeg commands, fallback export, normalization, concat, HTTP API,
or UI behavior.

- Preparation revalidates the current render identity, exact successful
  trajectory dependency, immutable saved workbench output, physical clip and
  authoritative frame map, project media specification, adapter identity, and
  the job-owned attempt directory before constructing manifest-free
  `RenderInputs`.
- Completion distinguishes stale input and adapter failure from validation or
  publication failure. A successful adapter result must provide output files
  inside the immutable attempt plus structured proof for exact source frame
  ordinals/PTS, time base, project media specification, monotonic output PTS,
  frame count, and artifact hashes.
- Validated artifacts are copied through a same-parent temporary directory and
  atomically published as an immutable render output revision. Attempt files
  and diagnostics remain intact; cancellation and invalid results publish no
  render state.
- Jobs and render state are published together with one operation ID. If an
  interruption occurs after durable publication but before the in-memory queue
  acknowledges it, the existing operation-intent reconciler restores the exact
  durable success proof instead of misclassifying it as an algorithm failure.
- Queue serialization preserves the structured validation proof and clears it
  whenever an old success becomes stale, superseded, cancelled, or retried.

Substage 2c TDD evidence:

- RED: prepare initially rejected `clip_render` as unsupported; completion
  initially accepted missing proof, could not persist proof, and marked a
  durably published result failed after a simulated interruption.
- Focused render-job tests: `24 passed`.
- Render/service/queue/repository/recovery/workbench/workflow/media related
  regression: `332 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

Crash coverage note: the render-specific suite injects an interruption after
both participant manifests are durable but before in-memory queue acknowledgement.
Generic repository recovery tests cover partial multi-manifest owner publication.
This substage does not inject process death during the artifact-directory
`os.replace`; atomic rename behavior and orphan staging cleanup remain integration
coverage for the later real media adapter stage.

### Substage 2c immutable-publication review closure

- Reusing an existing render revision now fails closed unless the target is a
  real directory and its video, frame map, and manifest are contained regular
  files rather than symlinks or missing/non-file replacements.
- The existing manifest must match the current project, clip, job, input
  revision/fingerprint, adapter name/version, output revision/fingerprint,
  schema, and validation proof exactly. Both media files are rehashed and must
  match the current validated proof before idempotent reuse.
- Tests first publish a real successful render, then mutate the video, remove or
  replace the frame map, or alter each durable identity field. Republishing is
  rejected and the single old success record remains distinguishable; no new
  success record is added. Exact untouched content is still reused idempotently.
- Staged files are fsynced before rename. The parent directory is fsynced after
  rename on hosts that support directory descriptors; Windows returns an
  explicit unsupported result rather than claiming directory durability.

Review-fix TDD evidence:

- RED: all `9` tamper/incomplete/identity-collision cases reused the existing
  revision and therefore failed their fail-closed assertions.
- Focused render-job tests: `33 passed`.
- Related regression: `332 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.
