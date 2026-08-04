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
- Stable quoted ETag; matching `If-None-Match` returns 304 with a strict empty
  body.
- Snapshot provides server-computed project and clip capabilities.
- `srt_full_pose` remains visible but reports interface-only/unavailable.
- Nullable workflow override restores the recommendation. Workflow changes
  stale old trajectory/workbench/render references without deleting them.
- Nullable custom display name restores the generated name.
- Batch preflight returns eligible/needs-confirmation/skipped per clip and can
  enqueue a partially accepted set.

### Project workspace

- Dark, responsive project workspace with exactly 项目概览、项目文件、片段管理、
  设置 in a collapsible icon-only sidebar.
- Product-facing scene/segment name, friendly time range and duration.
- Recommendation uses normal body text; current state and progress are separate.
- Optional measured fraction is shown; stage-only progress is used otherwise.
- Server capabilities exclusively control action availability.
- 1.5-second ETag polling preserves local workflow/name edits and selection.
- Workflow reset, inline rename, batch preflight/confirm, accessible live status,
  focus treatment, reduced-motion handling, and one merge action.
- Upload portal uses the new project endpoints in video → optional SRT → CAD
  order and immediately redirects to the project workspace. Existing legacy
  workflow endpoints remain served for old direct clients.

## Verification

- Upload/API/UI/old-upload/viewer focused set: `112 passed`.
- All project tests: `152 passed`.
- Related project/workflow/pure-rotation set: `371 passed`.
- Full suite: `711 passed, 1 skipped`.
- `compileall`: pass.
- `pyflakes` on all Task 4 changed Python files: pass. A wider repository-only
  audit still reports three pre-existing warnings in `json_repositories.py` and
  `test_recovery.py`; Task 4 does not change those files.
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
