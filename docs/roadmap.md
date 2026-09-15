# Roadmap

This document separates delivered behavior from future work. It is not a release promise. Current operation is documented in the root README and the [documentation hub](README.md).

## Current baseline

### Project pipeline

- The upload portal stores immutable video/CAD inputs, runs asynchronous analysis, segments source video into stable source-PTS clips, and opens Project Clip Management.
- The official upload UI accepts MP4 video, DXF drawings, and optional SRT. Compatibility validators may accept additional extensions, but those are not advertised as qualified portal formats.
- `sfm_only` is Stable and covers SfM, manual keyframes, initial/final route fitting, quality inspection, clip rendering, and strict frame-partition concatenation.
- Project state is split across project, clips, jobs, render, and annotations manifests with optimistic revisions, operation IDs, atomic JSON replacement, and restart reconciliation.
- The local resource queue reports preparation, computation, validation, and publication separately. A job reaches 100% only after validated owner publication.
- Workbench sessions are temporary write authorization; immutable workbench outputs preserve completed progress and allow the same clip to be reopened after refresh or service restart.
- Clip render revisions preserve source decoded-frame integer PTS, exact time base, output ordinal, and `render_frame_map.json`. Project concatenation rejects overlap, gaps, duplicate frames, and missing frames.

### CAD and engineering callouts

- DXF `TEXT`, `MTEXT`, and block-attribute text are retained in the 3D CAD view with layer, position, rotation, text size, and CAD-coordinate conversion. Visibility budgets protect large-drawing interaction.
- CAD-anchored engineering callouts are supported in the Render and Export workbench stage. They provide a screen-facing title/body card, polyline leader, circular anchor, style editing, time range, and draggable screen offset.
- CAD callout visibility is evaluated from the current effective Base/Corrected camera trajectory. Behind-camera, out-of-viewport, invalid-trajectory, and out-of-range points hide the whole callout.
- Callouts are burned into `rendered.mp4` without changing frame count or frame-map identity. Rendering without callouts retains the fast path.
- Global CAD replacement is available only after the project has a valid saved workbench output and the user confirms an unchanged coordinate system. It preserves analysis and trajectories, retains CAD-version history, and makes render/merge outputs stale.
- Service startup after CAD replacement verifies stored analysis jobs against immutable original input snapshots and rebinds them to the current project contract without rerunning analysis.

### Supported route with an immature classifier, and experimental routes

- `pure_rotation` is Supported. It runs a pinned external OpenGV backend followed by fixed-center global placement, local pose corrections, and rendering. Translation and scale are not recovered. The automatic motion classifier remains immature, so Project Clip Management retains human review and a final workflow override.
- The partial-SRT core is Experimental CLI functionality: PTS timing, local ENU conversion, robust Sim3 estimation, and fusion helpers exist outside the formal project queue.
- `srt_full_pose` is executable behind explicit per-project georeference and horizontal-FOV confirmation. It uses exact source PTS, emits a metric CAD-local trajectory, locks scale to 1.0, and does not require SfM or sparse points. Real-project surveying and height-datum validation remain necessary.
- `srt_sfm_fused` is Legacy read-only. Historical manifests and artifacts remain readable, but new projects no longer recommend, create, or enqueue it.
- SfM CUDA is Optional and limited to supported feature extraction and matching. Mapping and global bundle adjustment are not advertised as GPU processing.
- The video-target tracking annotation backend and immutable tracking revisions remain for automated tests and historical-data compatibility, but the creation UI is hidden and this is not a supported deliverable.

## Planned work

### Broader end-to-end acceptance

Continue real-project testing from upload through analysis, project queues, workbench resume, annotated/unannotated rendering, CAD replacement, service restart, and final concatenation. Promote behavior only after progress, recovery, media, and frame-map evidence remains stable across repeated runs.

### Video-target tracked callouts

Do not expose this control until a baseline reliably tracks real textured targets, hides immediately on lost/low-confidence/out-of-frame results, preserves gaps without interpolation, supports re-anchor revisions, and matches browser preview with final rendering. Cross-clip tracking is outside the current scope.

### Formal SRT/SfM integration

Connect the experimental partial-SRT core to a supported project route only after time synchronization, quality gates, failure recovery, and manual SfM-CAD alignment have been validated. Ordinary SRT remains supplementary metadata rather than precision position, pose, or CAD elevation truth.

### Full-pose SRT field acceptance

Expand real-project validation across additional DJI products, CGCS2000 zones, CAD axis conventions, horizontal FOV values, and independently surveyed height datums. The delivered synthetic end-to-end path proves execution and scale locking, not survey-grade accuracy for every camera or drawing.

### Motion-analysis reliability

Improve and validate the automatic `sfm_only` versus `pure_rotation` recommendation on broader real footage. Recommendation accuracy must remain distinct from the supported execution workflows, and the project page must retain a human override.

### Callout extensions

Possible later work includes robust video-target tracking, template libraries, layout collision avoidance, and evidence-based occlusion. Real-world semantic recognition, automatic CAD/video occlusion inference, cross-scene inheritance, and large neural models are not part of the current baseline.

### Broader acceleration validation

Validate optional CUDA installations and quality/performance trade-offs across supported COLMAP/pycolmap configurations. Mapping and global bundle adjustment remain outside advertised GPU coverage unless separately demonstrated.
