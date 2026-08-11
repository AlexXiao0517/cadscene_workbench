# Task 6: Standard-media clip render and verified project concat

## Scope

Implement Stage 8D/8E project-level per-clip rendering, explicit source-interval
fallback, media normalization, and final source-order concat. Reuse the existing
queue, adapter, repository, and project-service abstractions. Do not alter the
internal mathematics of `sfm_only`, `srt_sfm_fused`, `srt_full_pose`, or
`pure_rotation`.

## TDD order

1. Add failing pure tests for `ProjectMediaSpec`, ffprobe parsing, zero-start and
   monotonic output PTS, exact one-source-frame-to-one-rendered-frame validation,
   and render frame-map validation.
2. Add failing service/queue tests for per-clip render jobs, dependency and
   exclusive-key behavior, immutable attempts, stale-input/superseded handling,
   publication only after structured adapter validation, retry, and cancel.
3. Add failing tests for decoded-frame integer-PTS source fallback. The fallback
   must use `[source_start_pts, source_end_pts_exclusive)` and generate a frame map;
   it may not use floating-point `-ss/-t` coarse cuts or force a frame rate.
4. Add failing tests for source-order merge planning, incomplete-clip blocking,
   per-clip explicit fallback confirmation, media compatibility, normalization,
   concat pre/post validation, complete final frame map, and original-source audio.
5. Implement the smallest production changes, then add API and workspace UI tests
   for render, retry, merge preflight/confirmation, progress stages, and download.

## Required contracts

- The project owns one standard video media specification: display orientation is
  baked into pixels; width, height, SAR, pixel format, codec/profile, time base,
  frame timing, and color metadata are explicit and validated.
- A clip render is publishable only when `rendered.mp4` is zero-start,
  non-negative, monotonic, contains no synthetic or dropped video frame, and its
  decoded frame count exactly equals `render_frame_map.json`.
- `render_frame_map.json` maps every output frame in display/decode order to one
  authoritative source decoded-frame integer PTS. It is generated before encoding
  or from an already-authoritative upstream map, never inferred from output PTS.
- Render adapters return structured execution/progress/validation results and do
  not write any project manifest. Unknown progress remains stage-only; do not
  invent a percentage.
- Input revision/fingerprint is rechecked at publication. If it changes while the
  job runs, preserve attempt diagnostics, mark `superseded/stale_input`, and
  publish no current result.
- A merge plan follows clip `render_order` / source PTS order. Every unfinished
  clip blocks merge unless that exact clip has explicit user confirmation to use
  the original-video interval. Confirmation is revision-bound and cannot silently
  survive changed clip input.
- Original-video fallback uses the authoritative decoded frame index and integer
  PTS half-open interval. It produces a verified video segment and frame map using
  passthrough timing (`fps_mode passthrough` or `vsync 0`), without input-level
  pre-seek that changes timestamp semantics.
- Before concat, validate resolution, baked orientation, SAR, pixel format,
  codec/profile, time base, frame timing, color metadata, and audio parameters.
  Incompatible video is normalized to the project spec before simple concat.
- Segment video remains one-to-one with source frames. Concat inputs are zero-start,
  monotonic, and non-negative. The final frame map must equal the complete expected
  source decoded-frame PTS sequence exactly, with no duplicates or omissions.
- Do not concatenate per-clip audio. Mux audio once from the original long video,
  trimmed/mapped to the final video duration. Record measured audio/video duration
  delta and enforce tolerance
  `max(0.050, max_source_frame_duration_sec)`.
- Clip render revisions, merge plans, normalized intermediates, final outputs, and
  attempt logs are immutable. Atomic publish uses same-directory temporary output,
  validation, fsync, and replace. Adapters/subprocesses only write attempt outputs;
  the project service alone updates `render_manifest.json` and jobs state.
- Jobs explicitly save dependencies, resource class, exclusive key, idempotency
  key, input revision/fingerprint, adapter version, attempts, validation proof,
  and output revision. Use the existing single-machine resource-aware queue.

## API/UI boundary

- HTTP handlers only validate, call project services, and serialize responses.
- Snapshot capabilities are server-derived and revision/ETag aware.
- The project page exposes per-clip render/retry state and one project-level merge
  action. There must not be two competing “one-click merge” actions.
- Batch/preflight responses report individually eligible, confirmation-required,
  and skipped clips and allow partial success where appropriate.

## Required verification

- Focused: `tests/projects/test_media.py`, `test_render_jobs.py`, `test_concat.py`,
  project API and workspace static/UI tests.
- Related: project service/queue/adapters, video export/PTS, rendering, all four
  workflows, workbench sessions, and serve_viewer API suites.
- Full: entire pytest suite, Python compile check, JavaScript syntax check, and git
  whitespace/diff check.
- Real project verification remains Task 7; Task 6 must nevertheless include
  small FFmpeg integration fixtures when FFmpeg is available.
