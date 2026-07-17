# Upload Portal and Trajectory Workflow Router Design

## Goal

Add a standalone local upload portal that accepts required video and CAD files plus an optional DJI SRT file, detects the supported trajectory capability conservatively, records the result in the dataset manifest, and routes users into the existing viewer without changing the stable SfM/alignment/render algorithms.

## Scope

- Add `cadscene.srt` as a pure parsing and capability-detection package.
- Extend the existing dataset-import and HTTP workflow APIs with SRT upload and analysis retrieval.
- Add `apps/workflow_portal/` as a dependency-free static page.
- Preserve existing viewer upload APIs and no-SRT workflow behavior.
- Add only minimal synthetic SRT fixtures and automated tests.

Out of scope: SRT/COLMAP fusion, direct SRT camera paths, CUDA backend work, GPS-to-CAD projection, and new quality logic.

## Architecture

`workflow_portal` creates an opaque dataset/run identity through the existing server, uploads each file to the existing import endpoints, and sends SRT to a new endpoint. `cadscene.srt.parser` incrementally reads subtitle records and extracts known telemetry aliases; `cadscene.srt.capability` derives a conservative capability report and a `trajectory_mode`. `cadscene.workflow.data_import` persists the report paths and normalized workflow state in `dataset_manifest.json`. The existing viewer reads manifest-backed workflow state for display; URL parameters remain non-authoritative compatibility hints.

## Mode Classification

| Input evidence | Mode | Status |
| --- | --- | --- |
| SRT missing, malformed, or no valid GPS plus height coverage | `sfm_only` | `ready` |
| timestamp, GPS, and height available; no confirmed complete camera/gimbal attitude | `srt_sfm_fused` | `interface_only` |
| timestamp, GPS, height, and confirmed complete camera/gimbal yaw/pitch/roll | `srt_full_pose` | `interface_only` |

Drone-only attitude never qualifies as full pose. Aliases for latitude/longitude, altitude, gimbal attitude, camera attitude, and drone attitude are normalized before detection. Detection requires conservative coverage thresholds, produces warnings for poor GPS coverage and large SRT/video duration mismatch when video duration is known, and never blocks an already-ready video/CAD dataset on SRT parse failure.

## API and Manifest Contract

- `POST /api/workflow/upload-srt?dataset=<dataset>` accepts only `.srt` case-insensitively, streams to `data/<dataset>/telemetry/<safe filename>`, writes `srt_analysis.json` and `srt_analysis_report.md`, and returns normalized status, mode, analysis, and user-facing message.
- `GET /api/workflow/srt-analysis?dataset=<dataset>` returns the persisted analysis.
- New manifest `srt` data records availability, safe relative path, analysis path, source name, coverage, attitude kinds, and warnings.
- New manifest `workflow` data records automatic mode, optional debug override, and implementation status. Missing SRT is explicitly `sfm_only` and `ready`.

## User Experience

The normal portal shows only video (required), CAD (required), SRT (optional), per-file progress/status, detected user-facing mode text, short guidance, and an Enter Project button. It does not expose dataset/run IDs, scale/origin values, backend settings, or import asset formats. `debug=1` may reveal identifiers and guarded overrides. The button becomes enabled when video and CAD are ready regardless of SRT state. Entering the project opens the current viewer with dataset/run/mode hints; the viewer must display partial/full modes as unavailable interfaces rather than run unavailable algorithms.

## Testing and Safety

Tests are written first for parser modes, alias handling, malformed input, warnings, manifest persistence, upload endpoints, portal static behavior, and no-SRT API compatibility. Upload handling retains the existing multipart streaming and filename/path validation. No user SRT, video, CAD, project, data, or runs artifacts are committed.
