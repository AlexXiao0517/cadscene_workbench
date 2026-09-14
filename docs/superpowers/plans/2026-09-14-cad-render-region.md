# CAD clip-region rendering implementation plan

User approved exclusion of CAD outside the video's construction area on 2026-09-14,
including the far horizon clutter. This replaces full-drawing pixel parity; geometry,
color, labels and opacity inside the selected area must be unchanged.

Goal: select one static region for the entire clip, then cheaply reject invisible
spatial blocks per frame. Preserve relative altitude, smoothing and 1080p30 outputs.

Architecture: `cadscene/rendering/cad_region.py` owns validated XY bounds, automatic
whole-track extent inference, segment clipping, and a conservative spatial-block
visibility index. `calibrated_overlay.py` selects/clips once, indexes once, queries per
frame, and reports before/after counts and bounds. CLI exposes explicit local-coordinate
bounds, margin and lookahead. No new front-end controls are included in this backend pass.

Global constraints:
- One fixed region per clip, not a moving distance cutoff. No fade within region.
- Defaults: 100m margin, 1000m maximum horizontal ground-ray reach for automatic
  region inference. These are configurable heuristics, not surveyed site boundaries.
- Explicit bounds override auto inference and use the same local CAD-meter frame as
  the camera trajectory (after configured CAD origin/scale).
- Include crossing segments even when both original endpoints are outside; clip only
  at the static site boundary. Preserve ordering, colors and interpolated Z.
- Conservative block/frustum test for zero distortion; fall back to all regional
  segments for distorted cameras, followed by exact existing radial projection.
- Keep frame order, original video, original SRT and existing user worktree edits.
- Rendering artifacts use new names; old experiments can be moved into audit storage.

Tasks:
1. Failed-first unit tests for auto/manual region validation, long segment crossing,
   Z interpolation, far regional lines, visibility conservatism and distortion fallback.
2. Implement region/index module and wire production video renderer. Keep low-level
   frame renderer backwards compatible; add optional visibility index and precise
   screen bounds with anti-alias padding. Test optimized vs unindexed regional pixels.
3. CLI configuration + SRT render cache version bump with failed-first tests. This
   independent integration is delegated; main agent owns modules in tasks1/2.
4. Benchmark cached actual geometry for all three clips. Compare indexed regional
   output to unindexed regional output, inspect actual images, and record counts/time.
5. Replace stale full-CAD batch only after tests/probes pass. Use four processes for
   new regional renders with independent caches, verify metadata/full decode and QA,
   update the existing completion heartbeat to the new outputs/run.

Verification commands: `D:/anaconda3/python.exe -m pytest tests/rendering tests/cli/test_render_overlay_cli.py tests/projects/test_render_adapters.py -q` then full pytest.

Progress: implementation, CLI/cache integration, independent review and real probes
complete. Region/renderer/CLI/adapter targeted suite:107 passed. Full suite:2024
passed,5 skipped,6 test-server termination timeouts; affected API tests rerun with
normal process permissions:15 passed. No product code change for test cleanup.
Reviewer identified terrain slope preservation: now drape original XYZ first, then
clip XYZ; production helper regression passed.

All three real clips:1,529,846 source segments reduced to38,666/39,010/37,993,
167/168/167 labels. Nine real probes have exact indexed/unindexed regional pixel
parity, including four-process parity. First/middle/last sample images retained.
Old full-CAD parent103520 and verified children terminated. Old first completed
and second incomplete outputs moved recoverably into audit/superseded-full-cad.
New four-process regional batch runs as session30596, script
`tmp/regional_relative_render.py render`; automatically verifies all outputs after
render. Completion heartbeat automation-2 updated to these outputs. Final whole-video
verification and delivery remain pending. No commit or service restart performed.

Completion2026-09-14 17:28 CST: all three outputs passed complete FFmpeg decode,
expected frame counts,1080p30 metadata; all nine final QA images visually reviewed.
Render times332.33/272.93/356.46seconds,total16min2s excluding prep/verification.
Batch exited0; completion heartbeat pause requested. No originals overwritten.
