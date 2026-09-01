# Bundle-local Windows Path Budget Design

## Goal

Allow the Windows offline bundle to start and complete new-project video analysis from a normally nested installation directory while keeping the runtime, workspace, projects, and logs inside the bundle. The fix must not require `LongPathsEnabled`, a virtual drive, a junction, or an external user-data directory.

## Physical layout

- Keep the public project layout and logical identities unchanged.
- New video-analysis attempts write their mutable output below `v/` instead of `02_video_analysis/`.
- Immutable attempt revisions are stored below `v/r/` instead of `02_video_analysis/analysis_revisions/`.
- Revision directory names remain bounded hashes, while the full analysis revision remains in manifests and the current-revision pointer.
- Published project artifacts keep their existing layout because their bounded paths already fit the required budget.

## Compatibility

- Readers resolve the compact layout first and continue to accept the previous `02_video_analysis/analysis_revisions/` and flat revision layouts.
- Existing project manifests, job attempts, published artifacts, logical IDs, fingerprints, API payloads, and URLs are not migrated or renamed.
- Only newly generated attempt output uses the compact physical layout.

## Launcher behavior

- The launcher continues to reject paths that remain unsafe for native tools after compaction; it does not claim unlimited path depth.
- Its probe models the compact attempt layout and a legacy long project ID, so both new and existing project identifiers retain sufficient budget.
- The current `dist/releases/0.1.3/CADScene-0.1.3` nesting must pass the 240-character safety limit without moving `workspace` outside the bundle.

## Verification

- Add failing tests for compact publication, legacy-layout resolution, and the real nested 0.1.3 path budget.
- Run video-analysis, project-analysis, packaging, and PowerShell parser tests.
- Run the full test suite, dependency check, and `git diff --check` before rebuilding 0.1.3.
- Verify the rebuilt bundle from its nested release directory and confirm start/stop state uses the bundle-local workspace.

## Non-goals

- Do not enable Windows registry long-path policy.
- Do not add `\\?\` prefixes to third-party tool arguments.
- Do not relocate data, create drive letters, migrate old projects, or alter video-analysis behavior.
