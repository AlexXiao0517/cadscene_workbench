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
- Only the pre-existing fontTools deprecation warning remains.

## Non-goals retained

- No rendering, concat, source fallback, labels/tracking, scheduler expansion,
  cross-clip trajectory continuity, or workflow mathematics changes.
