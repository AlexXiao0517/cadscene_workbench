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

### Pure concat preflight, plan, and frame-map contracts

- Added a side-effect-free `cadscene.projects.concat` module. It does not import
  repositories, project services, queue state, or `QueueJob`, and performs no
  filesystem or subprocess work. `RenderCandidate` and `FallbackArtifact` are
  immutable canonical snapshots that the service must construct only after its
  authoritative job/owner/output validation.
- Clip `render_order` must be exactly `0..N-1`, unique, and identical to source
  PTS order. Exact source time bases, contiguous half-open intervals, per-clip
  authoritative frame maps, and the flattened complete source decoded ordinal /
  integer-PTS sequence are all validated before candidate preflight.
- Canonical rendered candidates bind project, clip, workflow, current input
  fingerprint, immutable output revision/fingerprint, proof fingerprint,
  publication operation, exact video/frame-map SHA-256 values, paths, probed
  media, and render frame map. Non-current
  candidates become explanatory per-clip blockers rather than current inputs.
- Source fallback confirmations bind project/clips revisions, clip analysis
  revision, interval fingerprint, source-asset fingerprint, and project media
  spec revision. Every field has an independent stale-confirmation regression.
  A valid confirmation with no artifact creates a truthful
  `dependency_required` entry with no fake paths; a ready fallback must also bind
  its current interval/source/media revisions and immutable output identity.
- Ready segment validation uses production rendered-media/frame-map and media
  compatibility contracts, additionally requiring source-equivalent frame
  PTS deltas. Packet duration remains diagnostic rather than authoritative;
  half-open segment durations come from the decoded source PTS contract and are
  written explicitly into the concat plan. Media differences become explicit `needs_normalize` fields; zero-start,
  negative/non-monotonic PTS or frame identity defects are blockers, not
  normalization candidates.
- A plan cannot be built while any blocker remains. It always names the original
  long video as its audio source, binds its source-asset fingerprint, and never
  consumes per-clip audio. The pure final
  frame-map builder rechecks that concatenated output ordinals map exactly to the
  complete authoritative source frame sequence with no duplicate or omission.

TDD evidence:

- RED: `tests/projects/test_concat.py` initially failed collection because the
  pure concat module did not exist.
- Focused concat contract: `28 passed`.
- Focused concat/media/source-fallback/render: `196 passed, 1 skipped`.
- Related project/render/service/PTS/export regression: `651 passed, 2 skipped`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

### Manifest-free concat execution boundary

- Added `ConcatMediaInputs`, immutable per-segment execution records, and a
  manifest-free `ConcatMediaAdapter`. The adapter accepts only a canonical
  `ConcatPlan`, the complete authoritative decoded-frame index, the project media
  spec, original source asset, and an existing attempt directory.
- Preparation rejects any dependency-required, unready, ambiguously normalized,
  or out-of-order entry. It re-hashes the original long video and every selected
  rendered video/frame-map file against the immutable plan; validation repeats
  those checks before and after the structured validator to detect changes while
  the task is running.
- Plan entries, source-frame sequences, and media-difference collections are
  normalized to immutable tuples. Validation is bound to the frozen execution
  snapshot rather than a caller-owned plan collection, and rechecks regular-file
  status so a source cannot be replaced by a same-byte symlink after preparation.
- The execution plan explicitly separates compatible inputs from normalization
  outputs, preserves source order and immutable publication identities, carries
  the complete expected final frame map, and declares only the original long
  video as the audio source. Expected video duration is derived from the complete
  source integer-PTS span; A/V tolerance is exactly the greater of 50 ms and the
  maximum authoritative source-frame duration.
- This substage defines no subprocess or persistence behavior. The next substage
  supplies the FFmpeg executor and production output validator; adapters remain
  unable to read or write project manifests.

TDD evidence:

- RED: concat adapter module absent; later regressions demonstrated that
  post-prepare byte replacement, unknown normalization state, and malformed
  render order were initially accepted.
- Focused concat adapter/plan/media: `96 passed, 1 skipped`.
- Related concat/source-fallback/render: `207 passed, 2 skipped`.
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
  publication failure. Adapter output files must remain inside the immutable
  attempt; the service independently probes and constructs the authoritative
  validation proof rather than accepting adapter claims.
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

### fa74dea media-trust and recovery review closure

- Production validation now invokes `ffprobe -show_streams -show_frames
  -show_format` through `probe_media()` and parses the result into the existing
  strict media contract. `ProjectService` accepts a probe callable only so pure
  unit tests can inject deterministic structured probe data.
- Frame count, output PTS, orientation and full project media compatibility are
  derived from the probed MP4. The service combines these facts with the exact
  authoritative frame map and actual artifact hashes to create the durable
  proof and output fingerprint; adapter-reported proof/fingerprint values are
  not trusted.
- Render identity now includes SHA-256 and byte size for both the physical MP4
  and authoritative frame map. The frame map's exact source time base must match
  the clip contract. The enqueue and current-fingerprint paths share this same
  identity builder, so either input changing supersedes the queued job.
- Restore revalidates every successful clip render against its immutable output
  manifest, contained regular files, actual hashes, frame map, server probe,
  project media specification and durable proof. Publication recovery performs
  the same check before accepting persisted success.
- Validation returns a frozen bundle of resolved attempt paths and server proof.
  Publication copies only those paths, then re-probes/re-hashes the staging
  video and frame map before `os.replace`; a post-validation source change
  cannot be published.
- Finish recomputes the complete current render-input fingerprint after server
  probing and again after artifact staging. A physical input changing during
  either interval ends as `stale_input` and publishes no current render state.

Review-fix TDD evidence:

- RED: the focused reviewer reproductions first failed because media probing was
  absent; the old path trusted adapter count/PTS, omitted physical-input hashes,
  restored a tampered success, and had no frozen staging revalidation boundary.
- Real media integration generated a two-frame MP4 with ffmpeg, verified it with
  production ffprobe, and confirmed arbitrary bytes are rejected: `1 passed`.
- Focused render/media tests: `96 passed`.
- Related regression: `340 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

### Substage 4b-2: real concat media executor

- Added a manifest-free executor and CLI that consume the frozen
  `ConcatMediaExecutionPlan` snapshot. The JSON loader is strict, restores no
  callable authority, and every source/segment hash is checked before, between,
  and after media commands.
- The execution snapshot also binds the canonical project-media-spec fingerprint
  and names its attempt directory. The CLI independently requires the scheduler's
  authorized attempt directory and rejects a self-declared snapshot root, changed
  spec, escaped sidecar path, or symlink root before any media command.
- Incompatible segments are normalized to the project media specification with
  passthrough frame timing, no forced frame rate, no frame synthesis/drop, no
  audio, no B-frame reordering, zero-start PTS, baked orientation, explicit
  color/SAR/time-base metadata, and post-encode frame-map validation.
- Compatible segments are stream-concatenated in source order. The concat
  demuxer list includes each authoritative source-interval duration; it does not
  infer the next segment offset from an isolated MP4's often-missing last-frame
  packet duration. This preserves a VFR gap that crosses a clip boundary.
- Before concat, the executor flattens the actual immutable per-segment frame maps
  and requires exact equality with the final authoritative map, preventing a
  duplicate/omitted segment map from being hidden by a correct generated sidecar.
- Per-segment audio is never concatenated. When the original source contains
  audio, it is trimmed once to the authoritative final video duration, rebased,
  encoded, and muxed without `-shortest`; a source without audio produces an
  explicit video-only result. Final media is checked for exact frame identity,
  zero/non-negative/monotonic PTS, VFR deltas, project media spec, exact duration,
  audio/video tolerance, and source/segment/output hashes.
- Commands write only to a random directory inside the immutable attempt.
  `final.mp4` and `final_frame_map.json` are fsynced inside one complete immutable
  bundle directory, then the directory is installed with one atomic rename after
  validation. A crash can therefore expose either neither output or both outputs,
  never a half-published pair; failed attempts retain their temporary diagnostics.
  Directory fsync is applied where the platform supports it. If parent-directory
  fsync reports an error after the atomic rename, the already complete bundle is
  not misreported as unpublished; restart recovery can revalidate that immutable
  directory. Progress is structured by stage and does not invent a fraction.
- All proof construction and output hashing completes before the first atomic
  bundle install; no fallible validation remains after publication.
- The standalone restart/recovery validator rechecks the frozen media-spec
  fingerprint and every source/segment byte binding before and after probing.
  It independently probes the authoritative long video to preserve the
  original-audio-only policy, so a substituted video-only final cannot be
  recovered as successful when the source contains audio.

TDD and real-media evidence:

- RED: the executor module did not exist; command, revalidation, atomic failure,
  video-only, strict snapshot, CLI, and final-timing tests failed at import.
- A real first concat lost an 80 ms VFR interval across the clip boundary when
  it relied on isolated MP4 duration. The final validator rejected it. Adding
  authoritative `duration` entries to the concat list produced exact final PTS
  `(0, 40, 120, 160)` at time base `1/1000` without relaxing validation.
- Real FFmpeg fixture: non-zero source PTS, irregular timing, adjacent half-open
  intervals, one 32-to-64-pixel normalization, source-order video concat, and one
  original-source audio mux. Final video/audio duration is 0.200 s and the final
  frame map exactly equals the complete source decoded-frame sequence.
- A dedicated fail-closed regression proves that a correct frame map cannot hide
  incorrect final PTS timing.
- Exact final video duration is the final output PTS plus that frame's packet
  duration; earlier packet durations are not summed and cannot hide an incorrect
  final-frame end. When the final packet duration is absent, the only accepted
  fallback is the video stream's integer `duration_ts` in its exact time base.
  Decimal stream duration and container duration remain diagnostic only.
- Focused executor: `22 passed`; concat/media/source-fallback focused:
  `151 passed, 2 skipped`; project plus PTS/export related regression:
  `627 passed, 3 skipped, 1 unrelated deprecation warning`; full suite:
  `1161 passed, 3 skipped, 1 unrelated deprecation warning`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.
- Independent final review: approved with no remaining Critical, Important, or
  Minor finding. The strict snapshot loader additionally rejects unsafe or
  duplicate segment `clip_id` values so proof maps cannot collapse identities.

Preflight integration closure:

- Pure concat preflight now treats packet duration as diagnostic, matching the
  decoded-frame PTS authority contract. It validates zero-start output PTS and
  all observable internal PTS deltas; the exact half-open segment duration comes
  from the authoritative source interval and is written into the concat list.
  Final output remains fail-closed on the complete source PTS sequence and exact
  authoritative total duration, so missing `pkt_duration` is supported without
  accepting a compressed or expanded final timeline.

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

### Render preflight and owner-consistency review closure

- Render preflight validates each clip's physical MP4 and authoritative frame
  map identity before declaring it eligible. A missing, malformed, or
  time-base-inconsistent asset is reported as a per-clip skip, while valid clips
  in the same request remain enqueueable; enqueue still rebuilds the identity.
- Current render fingerprint resolution treats missing/corrupt physical inputs
  as unverifiable and returns `None`, allowing restore to supersede only the
  affected task instead of aborting the whole project restore.
- A persisted successful render now additionally requires one exact owner record
  in `render_manifest`: render/job/project/clip identity, input/output identity,
  workflow/adapter, paths, proof, and operation ID must match the jobs owner's
  publication operation. Missing or mismatched owner state cannot restore a job
  as successful, and publication recovery uses the same validation.

Review-fix TDD evidence:

- RED: malformed per-clip media remained eligible, while missing/mismatched
  render owner records still restored jobs as success.
- New focused reviewer cases: `8 passed`.
- Focused render/media tests: `103 passed`.
- Related regression: `347 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

Final restore fail-closed hardening:

- Persisted render output revisions are revalidated with
  `is_safe_stable_id()` before any repository or filesystem lookup, so polluted
  traversal-like revisions cannot influence output path construction.
- A successful render whose clip no longer exists now fails persisted-output
  validation and restores as unsuccessful instead of raising `StopIteration`
  and blocking the entire project startup.
- Both focused regressions passed after failing before the guard changes.
  Focused render/media: `105 passed`; related: `349 passed`; static checks pass.

### Restore downgrade consistency and activation-window closure

- Restore now preflights every successful clip render before queue hydration.
  Invalid artifact/owner state becomes job `failed` plus render
  `failed_validation`; a changed current input becomes job `superseded` plus
  render `stale_input`. Jobs and render are downgraded through one recoverable
  `publish_manifests` operation and carry the same new operation ID.
- Downgrade preserves immutable output revision, fingerprints, published paths,
  proof, attempts, and logs as diagnostics. Only validation/current status is
  revoked. A missing owner record still advances both manifest owners under the
  downgrade operation without blocking project restore.
- Render owner records use an exact canonical key set; unexpected fields are not
  accepted as a current successful record.
- Both jobs/render candidate callbacks recheck the complete input fingerprint
  after `prepare_success_candidate`. Input changes in that window become
  `stale_input` before activation. Recovery also handles a jobs-only durable
  prefix: reconcile completes the prefix, then a second shared operation
  downgrades both owners, so no successful prefix remains current.

Review-fix TDD evidence:

- RED reproduced unsynchronized restore downgrade, a stale success created from
  the post-candidate hook, and acceptance of an extra owner-record field.
- Jobs-only durable-prefix recovery test: pass.
- Focused render/media/queue: `153 passed`.
- Related regression: `354 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

### Recovered terminal candidate contract hardening

- Queue commit now compares one shared immutable job contract for both prepared
  success and recovered terminal candidates: project/clip/job type, resource,
  priority, DAG/exclusivity/idempotency identity, input revision/fingerprint,
  adapter identity, attempt lease, submission provenance, and cleanup fields
  cannot change during publication recovery.
- A recovered candidate must be `failed` or `superseded` with matching stage,
  revoked output validation, and no validated-input fingerprint. Its operation
  and publication operation must be the same non-empty ID.
- Durable diagnostic outputs are accepted only with paired output
  revision/fingerprint, non-empty string path mappings, mapping-shaped proof,
  and mapping-shaped optional progress. An output-less terminal candidate cannot
  smuggle published paths or proof.
- The prepared-success commit also uses the immutable contract helper and now
  requires `stage == success`, paired non-empty output revision/fingerprint,
  validated output against the leased input fingerprint, matching non-empty
  publication operation IDs, and at least one well-formed published output.
  Validation proof remains optional because analysis adapters may not emit one;
  when present, proof and progress must be mapping-shaped.

Review-fix TDD evidence:

- RED: polluted clip ID, job type, adapter, stage, and output-validation state
  were all accepted by the recovered commit API.
- RED: five malformed prepared-success candidates (missing fingerprint,
  unvalidated output, wrong validated input, missing publication operation, and
  missing outputs) were accepted before the structural guard.
- Focused queue/render: `107 passed`.
- Related regression: `364 passed`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

### Decoded-frame integer-PTS source interval fallback

- Added a standalone `SourceIntervalRenderInputs` contract, manifest-free
  `SourceIntervalRenderAdapter`, and `cadscene.cli.render_source_interval` CLI.
  This is a merge fallback media adapter, not a fifth user workflow and not a
  value written to `resolved_workflow`.
- The adapter/CLI probes the complete presentation-order decoded-frame index and
  reuses the Stage 8A `ExportClip`/`build_clip_frame_map` half-open contract.
  `render_frame_map.json` is atomically written before encoding and maps output
  ordinal to authoritative source decoded ordinal and integer PTS.
- FFmpeg receives the original long video without input-level seek, trims with
  absolute integer `start_pts`/`end_pts`, resets only output PTS, uses
  `fps_mode passthrough`, and emits video-only output. It does not use `-ss`,
  `-t`, `-r`, or a forced frame rate. Auto-rotation is baked through the filter
  path and rotation metadata is cleared.
- H.264 output is configured to the explicit project resolution, SAR, pixel
  format, profile, time base, and color metadata. Exact MP4 time base is enforced
  with encoder time base plus track timescale and then checked by production
  `probe_media`, `validate_rendered_media`, and `media_compatibility`; unsupported
  codec or non-unit time-base numerator fails explicitly.
- A project media spec with `nominal_frame_rate=None` now denotes VFR timing and
  does not compare container-derived average frame rate. This avoids violating
  one-source-frame/one-output-frame timing with forced CFR; all other media fields
  remain strict.
- The service exposes a manifest-free plan method backed by the independent
  adapter. Neither adapter nor CLI writes project manifests; verified attempt
  outputs are only `rendered.mp4` and `render_frame_map.json`. Adapter commands
  carry the real project ID, preset/CRF fail at adapter construction, and the
  public command builder fixes its destination inside the attempt directory.
  CLI time bases require an explicit positive numerator/denominator rational;
  decimal, boolean-like, zero numerator/denominator, and either negative
  component fail closed. FFmpeg/FFprobe adapter values intentionally remain
  unresolved at construction so deployments may supply PATH command names;
  execution and validation still fail explicitly if they cannot be resolved.

TDD evidence:

- RED: the source fallback module/CLI did not exist; decoded-frame selection,
  safe FFmpeg command construction, pre-encode frame-map generation, structured
  adapter validation, and service invocation initially failed at import.
- RED: real adjacent VFR interval validation exposed inappropriate strict
  comparison of container-derived average frame rate when the project leaves the
  nominal rate unspecified.
- Real FFmpeg/CLI fixture: non-zero source PTS and irregular frame deltas; two
  adjacent half-open outputs flatten to the exact full source ordinal/PTS
  sequence, the boundary PTS appears once, both outputs start at zero, contain no
  audio, and probe at exact project time base.
- Focused source fallback/media: `81 passed`.
- Related source/media/render/service/PTS/export regression: `449 passed, 1 skipped`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.

#### Default edit-list encoder delay and atomic sidecar hardening

- A normal libx264 MP4 using its default edit list reproduced a fallback output
  whose first visible PTS was `1024` when output edit lists were disabled. The
  fallback now encodes with `-bf 0` while retaining `-use_editlist 0`, eliminating
  reordering delay without relaxing zero-start validation, introducing negative
  timestamps, forcing a frame rate, or adding/dropping frames.
- Real regressions cover both an ordinary source and a stream-copy source with a
  90-degree display-rotation matrix. Both fallback outputs start at integer PTS
  zero, remain strictly monotonic and non-negative, contain exactly the mapped
  decoded-frame count, bake display orientation, and match project dimensions.
- Atomic frame-map writes now use a same-directory random file created by
  `tempfile.mkstemp` with exclusive creation. The open descriptor is flushed and
  fsynced, the path is checked as non-symlink and against its original file
  identity before `os.replace`, and cleanup only removes that exact created file.
  A legacy fixed temporary symlink/residual is never opened or removed; concurrent
  writers use distinct names. The symlink-specific assertion skips on Windows
  hosts without symlink creation privilege, while residual/concurrency and
  replaced-file ownership tests run there.

Review-fix TDD evidence:

- RED: both default-edit-list real sources failed strict validation because the
  rendered first frame did not start at zero; the command lacked `-bf 0`.
- RED: concurrent fixed-name frame-map writers collided on Windows, and the old
  fixed path was vulnerable to following a precreated symlink where supported.
- Focused source fallback/media: `85 passed, 1 skipped`.
- Related source/media/render/service/PTS/export regression:
  `453 passed, 2 skipped`.
- `pyflakes`, `compileall`, and `git diff --check`: pass.
