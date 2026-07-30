# Changelog

## Unreleased

### Added

- Added the upload portal and workflow routing for video, CAD, and optional SRT inputs.
- Added conservative SRT capability detection and the experimental partial-SRT core for PTS, ENU, robust Sim3, and fusion processing. Ordinary SRT remains a metadata capability source, not high-precision position, pose, or CAD elevation truth.
- Added the experimental `pure_rotation` workflow for fixed-camera-center footage through the external OpenGV backend. It does not recover translation or scale, and users select it explicitly rather than through automatic motion classification.

### Changed

- Improved CUDA capability detection and reporting for supported feature extraction and matching, mapper progress reporting, bounded bundle adjustment, and explicit CPU fallback when CUDA cannot be confirmed. Mapper and global bundle adjustment are not represented as GPU processing.
- Improved the SfM-CAD alignment baseline, manual keyframe FOV priority, and validation of degenerate results. Confirmed, mutually consistent manual-keyframe FOV values take priority; FOV does not guarantee SfM accuracy.
- Improved keyframe handling, viewer behavior, Unicode upload handling, workflow buttons, and `--storage-root` handling for workflow data and artifacts. `--root` continues to serve static resources.

### Limitations

- `sfm_only` is the only stable end-to-end primary path.
- The `srt_sfm_fused` portal route is interface-only: it can be recognized and described, but formal workflow launch is blocked.
- `srt_full_pose` is interface-only and has no end-to-end execution workflow.
- The partial-SRT core is an experimental CLI capability and is not connected to the formal job runner.
- CAD readiness for DWG depends on external conversion tools. CUDA coverage does not establish GPU mapper or global bundle-adjustment support.

## v0.1.0-road-sfm-only

- Complete no-SRT workflow.
- Local video and CAD data upload.
- pycolmap/COLMAP SfM.
- Manual keyframe calibration.
- SfM-to-CAD alignment.
- Quality checks and frame supplementation guidance.
- Viewer scene generation.
- Road-surface diagnostics.
- Video render, preview, and download.
- Frontend job runner.
