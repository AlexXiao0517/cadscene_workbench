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
  output-count equality, ordered unique source identities, and optional exact
  equality with an authoritative `DecodedFrameTimestamp` sequence.
- `validate_rendered_media()` combines decoded output count/PTS validation with
  the frame-map proof and rejects unbaked output orientation.
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
