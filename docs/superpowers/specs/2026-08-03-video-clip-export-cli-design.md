# Video Clip Export CLI Design

## Goal

Provide an independent, explicitly invoked Python CLI that converts a Stage 8A
`clip_manifest.json` into physical preview MP4 files. This is a validation tool,
not part of automatic video analysis or workflow execution.

## Interface

```text
python -m cadscene.cli.export_video_clips \
  --video SOURCE.mp4 \
  --manifest clip_manifest.json \
  --output-dir physical_clips \
  [--ffmpeg PATH] [--preset fast] [--crf 18]
```

The CLI prints the published output directory and one summary line. It returns a
non-zero exit code for invalid input, an unsafe manifest, unavailable FFmpeg, or
any failed clip export.

## Input Contract

- The source video and manifest must exist.
- `clips` must be a non-empty list.
- Every clip must have a safe `clip_id`, finite numeric
  `source_start_pts_sec`/`source_end_pts_sec`, and a positive duration.
- Source PTS ranges must be ordered, non-overlapping, and strictly below the
  60-second Stage 8A limit.
- Output filenames are derived only from validated `clip_id` values.

## Export Behavior

- Resolve FFmpeg through the existing video-analysis runtime unless `--ffmpeg`
  is supplied.
- Seek and encode each range with H.264 (`libx264`), `yuv420p`, optional AAC
  audio, `+faststart`, the requested preset, and CRF.
- Use manifest PTS seconds directly; never derive frame numbers from FPS.
- Re-encode rather than stream-copy so preview boundaries are not displaced to
  keyframes.
- Write all clips into a sibling temporary directory. Validate return codes and
  output files, then atomically publish the complete directory. A failed run
  must not expose a partially written final directory.
- Refuse to overwrite an existing output directory unless a future explicit
  overwrite option is designed; the first version has no overwrite flag.

## Separation of Concerns

Core validation and export orchestration live in a small
`cadscene.video_analysis.clip_export` module. The CLI module only parses
arguments and reports results. The analyzer, logical manifest generation,
workflow recommendation, SfM, SRT, and Pure Rotation code remain unchanged.

## Testing

- Unit tests cover manifest validation, unsafe IDs, invalid/overlapping ranges,
  the strict duration limit, and existing output refusal.
- CLI help is tested independently.
- An FFmpeg integration test creates a small synthetic video, exports logical
  ranges, probes the resulting MP4 files, and verifies count and approximate
  durations.
- Final manual acceptance exports the current `jinhuaorigin.mp4` manifest and
  verifies 11 playable files with the expected ordering.

## Deferred Integration

No analyzer flag, project-management integration, automatic invocation,
background scheduling, concat, or workflow execution is included. Integration
will be considered only after this standalone CLI and real-video export pass.
