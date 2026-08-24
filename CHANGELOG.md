# Changelog

## Unreleased

### Added

- Added a local project library with fixed-width folder cards, a detailed list view, durable view preference, safe project summaries, project reopening, optimistic renaming, and navigation back to the existing upload flow.
- Added packaged official web/config resources, complete runtime dependency declarations, and the unified `cadscene-workbench serve` / `cadscene-workbench doctor` console entry point.
- Added the official upload portal for MP4 video, DXF CAD, and optional SRT inputs. Extra extensions accepted by compatibility-oriented backend validators are not exposed as supported portal formats.
- Added the persistent project pipeline: asynchronous video/CAD analysis, scene-aware clip management, workflow selection, resource-aware trajectory and render queues, resumable workbench sessions, immutable clip renders, and strict frame-partition project concatenation.
- Added DXF `TEXT`, `MTEXT`, and block-attribute text to the 3D CAD view, preserving layer, position, rotation, text size, and CAD-coordinate conversion while applying visibility budgets for large drawings.
- Added Stage 9 CAD-anchored engineering callouts with editable title/body, screen-space panels, polyline leaders, circular anchors, source-PTS visibility, Base/Corrected camera projection, browser preview, persistence, and burn-in rendering.
- Added guarded global CAD replacement for calibrated projects. A same-coordinate-system replacement preserves analysis, trajectories, keyframes, and workbench outputs while making CAD-dependent render and merge outputs stale.
- Added conservative SRT capability detection and the experimental partial-SRT core for PTS, ENU, robust Sim3, and fusion processing. Ordinary SRT remains a metadata capability source, not high-precision position, pose, or CAD elevation truth.
- Added the supported `pure_rotation` workflow for fixed-camera-center footage through the pinned external OpenGV backend. Conservative automatic motion analysis can recommend it and Project Clip Management allows a final override; the classifier remains immature and the route does not recover translation or scale.

### Changed

- Improved CUDA capability detection and reporting for supported feature extraction and matching, mapper progress reporting, bounded bundle adjustment, and explicit CPU fallback when CUDA cannot be confirmed. Mapper and global bundle adjustment are not represented as GPU processing.
- Improved the SfM-CAD alignment baseline, manual keyframe FOV priority, and validation of degenerate results. Confirmed, mutually consistent manual-keyframe FOV values take priority; FOV does not guarantee SfM accuracy.
- Improved keyframe handling, real project-task progress, viewer/workbench resume behavior, Unicode upload handling, workflow buttons, render publication, and `--storage-root` handling. `--root` continues to serve static resources; current writable project state lives under `<storage-root>/projects/`.
- Rendering with no active callouts retains the original fast path. Rendering with callouts preserves the authoritative output frame count and `render_frame_map.json`; final concatenation rejects duplicate or missing source frames.

### Fixed

- Fixed service startup after global CAD replacement. Persisted analysis jobs are verified against their immutable original input snapshots and safely rebound to the current project contract, without rerunning analysis or editing existing project artifacts.
- Fixed stale workbench-session restoration and completed-clip status handling so reopening a calibrated project does not erase durable progress.

### Limitations

- `sfm_only` and fixed-center `pure_rotation` are executable end-to-end paths. Automatic motion classification still requires human review.
- The `srt_sfm_fused` portal route is interface-only: it can be recognized and described, but formal workflow launch is blocked.
- `srt_full_pose` is interface-only and has no end-to-end execution workflow.
- The partial-SRT core is an experimental CLI capability and is not connected to the formal job runner.
- The video-target tracking annotation backend remains available for tests and historical-data compatibility, but its creation control is hidden and it is not a supported user-facing capability.
- CAD-anchored callouts do not infer real-world occlusion in source video. Cross-clip tracking, cross-scene annotation inheritance, semantic recognition, and neural tracking models are not implemented.
- The official project upload UI currently supports DXF rather than DWG. Compatibility-only DWG readiness depends on external conversion tools. CUDA coverage does not establish GPU mapper or global bundle-adjustment support.

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
