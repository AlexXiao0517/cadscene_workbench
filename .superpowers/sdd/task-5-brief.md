# Task 5 brief: workbench session save and return

## Scope

Implement the approved Stage 8D workbench round trip on top of Task 4 without
changing the internals of `sfm_only`, `srt_sfm_fused`, `srt_full_pose`, or
`pure_rotation`.

## Files

- Create `cadscene/projects/workbench_sessions.py`.
- Modify `cadscene/projects/http_api.py` and only the minimal service/repository
  seams required to coordinate clip state.
- Modify `apps/project_workspace/project_workspace.js`.
- Modify `apps/web_camera_viewer/workflow.js`.
- Add `tests/projects/test_workbench_sessions.py` and extend the project API and
  static UI tests.

## Required contracts

1. A workbench session is bound to project ID, permanent clip ID, resolved
   workflow, clip/project input revision and fingerprint, trajectory run/output
   identity, save permissions, expiry, and an allowlisted same-origin
   `return_to` path.
2. `return_to` must be a local allowlisted project-workspace path; reject
   schemes, hosts, protocol-relative URLs, backslashes, traversal, encoded
   traversal, and unrelated local paths.
3. Session tokens are unguessable. Persist session state through an abstract
   `WorkbenchSessionStore`; the first implementation uses project-local atomic
   JSON with process locking, schema version, revision, and updated time.
4. Creating/opening a session is allowed only from server capabilities and a
   successful, current trajectory output for the clip/workflow/input identity.
5. Saving first invokes/validates the existing workbench output contract, then
   creates an immutable `workbench_output_revision`. Only the coordination API
   may transition the clip workbench state to `saved`; the workbench must not
   write project manifests directly.
6. A stale input, changed workflow, changed run, expired token, missing save
   permission, replayed final save, or invalid adapter output must fail closed
   and must not publish a current successful revision.
7. Old workbench outputs remain immutable. Workflow/input changes mark prior
   workbench/render results stale without deleting them.
8. Expired or browser-abandoned unsaved `editing` sessions recover to `ready`,
   never to success. A valid immutable saved revision remains saved.
9. The API must expose session create, session inspect/bootstrap, coordinated
   save, and close/abandon operations through thin handlers. Use expected
   revision where a manifest is mutated and preserve operation IDs.
10. The project snapshot returns server-derived `can_open_workbench` and current
    workbench state/revision; the front end does not infer capability.
11. Project page open action creates a session and navigates to the existing
    workbench. The workbench bootstraps from the token, calls coordinated save
    after its existing save succeeds, and returns to the allowlisted path with
    the originating clip focused. Expired/closed unsaved editing returns to
    ready.

## TDD order

1. RED: binding, token/expiry, allowlist, stale input/workflow/run, permission,
   immutable/replayed save, invalid output, abandon/recovery, and atomic-store
   tests.
2. GREEN: store, session coordinator, validation boundary, and immutable output
   publication.
3. RED/GREEN: thin HTTP routes, snapshot capabilities/state, ETag revision, and
   revision conflicts.
4. RED/GREEN: project-page navigation and workbench bootstrap/save/return static
   contracts while preserving existing workflow behavior.
5. Run focused, project, workflow/pure-rotation, viewer, and full regressions;
   write `task-5-report.md`; commit once; do not start Task 6.

## Non-goals

- No rendering/concat (Task 6), physical bulk export, task scheduler changes,
  cross-clip trajectory continuity, labels/tracking, or workflow math changes.
