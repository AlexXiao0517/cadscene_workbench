# Task 5 Report — Workbench session save and return

## Scope

- Added the Stage 8D project-to-workbench round trip without changing the
  mathematical internals of `sfm_only`, `srt_sfm_fused`, `srt_full_pose`, or
  `pure_rotation`.
- Rendering and concat remain Task 6 and were not implemented.
- The four business manifests remain the only project state owners. Session
  credentials are independent per-token-hash atomic records under
  `workbench_sessions/`, not a fifth project manifest.

## TDD evidence

1. Domain RED failed collection because `cadscene.projects.workbench_sessions`
   did not exist.
2. Domain GREEN covered complete binding, unguessable token records, expiry,
   strict return allowlisting, stale input/workflow/run/output, permission,
   immutable save/replay, invalid output, abandonment, and atomic writes.
3. A publication cleanup regression was reproduced with an escaped temporary
   directory; containment is now resolved and verified before any write or
   cleanup, and cleanup never walks the process cwd.
4. API RED failed because `ProjectWorkbenchService` and session routes did not
   exist. GREEN added thin create/inspect/save/close routes, optimistic clips
   revision checks, snapshot state, capabilities, and ETag behavior.
5. Failure-injection RED/GREEN covered output-published/session-write-failed and
   session-saved/clip-reference-write-failed windows. Retries preserve one
   operation and immutable revision, verify existing bytes, and repair only the
   exact missing reference.
6. Historical trajectory, real input mutation, expired ETag, pending-close,
   pagehide/save, and competing-session RED/GREEN tests close fail-open race
   paths.
7. UI RED/GREEN covers project session navigation, clip focus on return,
   workbench bootstrap, existing-save-first coordination, close-on-abandon,
   and Pure Rotation completion.

## Persistence and ownership

- `WorkbenchSessionStore` is an abstract credential-record boundary.
- `AtomicWorkbenchSessionStore` stores one JSON record per SHA-256 token hash;
  every record includes `schema_version`, `revision`, `updated_at`, and
  `operation_id` and uses same-directory temp write, fsync, and atomic replace.
- The raw token is never used as a filename or written into a business
  manifest.
- Current workbench state and immutable output revision are owned by the clip's
  `clips_manifest` reference. Workflow/input changes therefore use the existing
  stale-reference behavior without deleting old outputs.
- The same `operation_id` appears in the pending/saved session record, immutable
  output manifest, and current clip reference.

## Binding and fail-closed rules

- Sessions bind project, permanent clip ID, resolved workflow, active project
  analysis revision, clip analysis revision, exact input fingerprint,
  trajectory job/run, validated trajectory output revision/fingerprint, save
  permissions, expiry, and allowlisted return path.
- Initial and subsequent authority checks require the current job input
  fingerprint to match the server-recomputed fingerprint and require
  `job.input_revision == clip.analysis_revision`; a historical success or a
  changed manual/input definition cannot open or save.
- `return_to` accepts only `/apps/project_workspace/` with the bound `projectId`
  and optional bound `focusClip`. Schemes, hosts, protocol-relative URLs,
  backslashes, traversal (including repeated URL decoding), unrelated paths,
  and extra query keys are rejected.
- A second live session cannot replace an unexpired editing or recoverable
  pending session. Expired pure editing may be replaced. Old sessions cannot
  save or close over a newer clip reference.

## Recoverable immutable save

- A validated save first advances its credential record to `pending_save`,
  fixing the output revision, source revision/fingerprint, and operation ID.
- Immutable publication is atomic and content-addressed by the fixed revision.
- If publication succeeded but the session write failed, a retry with the same
  receipt revalidates the existing manifest identity and fingerprint and
  completes the same operation. A different receipt or unverifiable output is
  rejected without overwrite.
- If the session became saved but the clips publication failed, the same token
  can repair the exact missing reference. An already matching reference is a
  replay; a conflicting reference fails closed.
- `pending_save` is never downgraded to ready by close/abandon and remains
  recoverable. Only unsaved `editing` expires or closes to ready.

## Output validation

- General/SRT workflows accept only the server's exact bound
  `01_keyframes/camera_track_manual.json`, parse the JSON contract, and hash the
  server file. Client paths outside the bound run are rejected.
- Pure Rotation ignores a client-supplied path. It selects the bound run's
  corrected camera track first and otherwise the saved placement base track,
  then validates mode/nonempty poses/required pose fields and hashes the server
  file.
- Every successful save creates an immutable `workbench_output_revision`; only
  the coordination service publishes `saved` into clips state.

## API and UI

- Routes: create session per clip, inspect/bootstrap, coordinated save, and
  close/abandon.
- Snapshot returns server-derived `can_open_workbench`, workbench state, and
  output revision. Snapshot ETag includes effective workbench state, so crossing
  expiry cannot incorrectly return 304 while four manifest revisions remain
  unchanged.
- The project page creates a session from the server capability and navigates to
  the existing workbench. Return focuses and scrolls the originating clip.
- The workbench bootstraps from the token, runs the existing save first, then
  performs coordinated save and navigates to the allowlisted return path.
  `pagehide` does not close while a save is in flight.
- Pure Rotation completion coordinates after placement/correction output has
  been saved and refreshed.

## Verification

- Workbench/API/UI focused: `109 passed` before final additions; final focused
  suites are included in the broader run below.
- Project + workflow + Pure Rotation + viewer + project API: `543 passed`.
- Full suite: `894 passed, 1 skipped`.
- `python -m pyflakes` on changed Python modules: pass.
- `python -m compileall -q`: pass.
- `node --check` on both changed JavaScript files: pass.
- `git diff --check`: pass.

## Second review closure (2026-08-04)

- Saved-reference repair now re-resolves the complete current context and
  applies the same binding checks as a normal save. A reference whose status is
  `stale` is never repairable through save or close, does not block a new
  session, and an old token cannot replace the new session reference.
- Manual and Pure Rotation validators each read an artifact into one immutable
  byte snapshot. JSON decoding, structural validation, SHA-256, and immutable
  publication all consume those exact bytes, so a later legal replacement of
  the mutable run path cannot change what is published.
- Placement publication, correction publication, and Pure Rotation workbench
  validation share a normalized per-run process lock. Corrections read the base
  once, derive the corrected result and base hash from that snapshot, and
  atomically replace corrected/lineage files under the lock. A crash-visible
  partial generation remains fail-closed because lineage mismatch selects the
  current base instead of the corrected track.
- Snapshot revision now hashes the complete stable server response excluding
  only `snapshot_revision` itself. Project and clip capabilities therefore
  invalidate ETag even when the four manifest revisions do not change, such as
  when an authoritative media file disappears.
- Atomic session JSON replacement now fsyncs its parent directory after
  `os.replace`; the existing platform-compatible directory fsync helper keeps
  unsupported platforms fail-safe.
- No project manifest was added or reassigned. Four-manifest ownership,
  `operation_id` recovery correlation, fixed output containment, and the
  allowlisted return path remain unchanged. Rendering and concat Task 6 code
  was not touched.

Second-review TDD and verification:

- Critical stale save/close/open regression: observed `3 failed`, then
  `4 passed` including the existing legitimate saved-reference repair test.
- Manual/Pure byte-snapshot TOCTOU regression: observed `2 failed`, then
  `4 passed` including existing save paths.
- Capability ETag and session-directory fsync regressions: observed `2 failed`,
  then `3 passed` including stable 304 behavior.
- Pure Rotation single-base-snapshot regression failed because the coherent
  publisher did not exist, then passed with the real HTTP lineage and validator
  regressions (`4 passed`).
- Combined second-review critical regression set: `9 passed`.
- Task 5 focused: `157 passed`.
- Projects + all serve_viewer APIs + viewer + workflow + Pure Rotation:
  `588 passed, 1 warning`.
- Final fresh full suite: `910 passed, 1 skipped, 1 warning`. An earlier full
  run saw one pre-existing asynchronous JobRunner assertion race; the isolated
  test immediately passed and the final complete run was clean.
- `pyflakes` on changed production and focused task modules: pass. The complete
  legacy `test_serve_viewer_workflow_api.py` still reports only its two
  pre-existing duplicate test definitions (now at lines 418/512 and 438/475).
- `python -m compileall -q cadscene`,
  `node --check apps/web_camera_viewer/workflow.js`, and `git diff --check`:
  pass.
- Only the pre-existing fontTools deprecation warning remains.

## Non-goals retained

- No rendering, concat, source fallback, labels/tracking, scheduler expansion,
  cross-clip trajectory continuity, or workflow mathematics changes.

## Third review remediation (2026-08-04)

- The editing TTL now applies only while a session is actually `editing`.
  `pending_save` survives the original editing expiry and can recover either an
  already-published immutable revision or a missing revision after the current
  receipt is revalidated against its pending source revision/fingerprint.
- Project workbench inspect/bootstrap now runs under the project state guard,
  resolves the current clip and complete workbench context, and validates the
  current clip-owned reference before returning a credential. Changed inputs or
  workflows, stale/missing references, and old tokens displaced by a replacement
  session therefore fail closed before the old workbench can bootstrap.
- Legitimate saved-reference recovery remains available only for a current,
  non-stale reference with the same token and bound workflow/input/trajectory.
  Expired unsaved editing still resolves to `ready`.

Third-review TDD and verification:

- New pending-expiry and stale-bootstrap tests first produced `6 failed`, then
  passed together with legitimate saved repair and editing-expiry coverage
  (`9 passed`).
- Task 5 focused: `168 passed`.
- Projects + CLI + viewer + workflow + Pure Rotation: `629 passed, 1 warning`.
- Full suite: `914 passed, 1 skipped, 1 warning`.
- `python -m compileall -q cadscene`, JavaScript syntax checks for the project
  workspace and camera workbench, and `git diff --check`: pass.

Third-review narrow recheck closure:

- Entering `pending_save` now persists a canonical SHA-256 identity of the exact
  accepted receipt. Every recovery request must match that identity before an
  existing immutable target can be trusted or a missing target can be rebuilt.
  This preserves recovery without consulting a later mutable run file while
  rejecting `ok: false`, changed revisions, extra/different receipt data, and
  other replay mismatches.
- The published-target wrong-receipt regression first produced `2 failed`, then
  passed with the correct published/missing-target and mutable-source recovery
  cases (`7 passed`).
- Final Task 5 focused: `170 passed`.
- Final full suite: `916 passed, 1 skipped, 1 warning`.
- `pyflakes`, `compileall`, both JavaScript syntax checks, and `diffcheck`: pass.

## Review closure (2026-08-04)

- Immutable publication now copies the validated camera-track bytes into
  `workbench_outputs/<revision>/artifacts/`. The manifest contains only the
  revision-relative path, SHA-256, and byte size; it does not retain the
  mutable `runs/` path.
- General/SRT camera tracks now require a positive finite FPS, at least one
  keyframe, unique non-negative integer frame indices, finite optional time,
  and finite `x/y/z/yaw/pitch/roll/fov` camera values with a valid FOV range.
- Recovery closes the output-publication/session-record crash window. When a
  pending revision already exists, retry validates its operation ID, binding,
  source revision/fingerprint, manifest fingerprint, artifact containment,
  and artifact hash without consulting a later mutable `runs/` file. Only a
  missing revision revalidates the current receipt, which must still match the
  pending source revision/fingerprint.
- Snapshot and capabilities derive `pending_save`, `recovery_required`, and
  `saved` from the credential plus exact clip reference, so an expired editing
  timestamp cannot hide an outstanding recoverable operation or yield a stale
  304 response.
- Pure Rotation corrections now publish `correction_lineage.json` with hashes
  of the exact base and corrected files. Validation uses a corrected track only
  when both hashes match; otherwise it safely selects the current base track.
  Re-saving placement from the UI re-applies existing corrections before final
  coordination.
- Intermediate camera-track saves no longer coordinate the project session or
  navigate away. Explicit quality/Pure Rotation completion waits for bootstrap,
  performs the existing workflow save, coordinates the immutable save, and only
  then returns to the allowlisted project path.

Review verification:

- Real corrections HTTP endpoint lineage: `1 passed`.
- Workbench/schema/lineage/UI targeted: `115 passed`.
- Projects + all serve_viewer APIs + viewer + workflow + Pure Rotation:
  `580 passed, 1 warning`.
- Full suite: `902 passed, 1 skipped, 1 warning`.
- `pyflakes` on changed production and focused task modules: pass. Running it
  on the complete legacy `test_serve_viewer_workflow_api.py` additionally
  reports its two pre-existing duplicate test-function definitions (lines
  370/464 and 390/427); these unrelated tests were not renamed in this task.
- `python -m compileall -q cadscene`: pass.
- `node --check apps/web_camera_viewer/workflow.js`: pass.
- `git diff --check`: pass.
