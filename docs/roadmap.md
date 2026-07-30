# Roadmap

This document separates functionality already present in the workbench from
work that is still future work. It is not a release promise.

## Current baseline

- The upload portal, dataset manifest, conservative SRT capability detection,
  and route prompts are available now.
- `sfm_only` is **Stable** and is the only stable end-to-end JobRunner route.
- `pure_rotation` is **Experimental**. It can run the external OpenGV
  rotation-recovery backend, then use manual placement and local pose
  corrections. The camera centre is fixed: translation and scale are not
  recovered.
- The partial-SRT core is **Experimental CLI** functionality: PTS timing,
  local ENU conversion, robust Sim3 estimation, and fusion helpers are
  available outside the formal JobRunner route.
- The `srt_sfm_fused` portal route is **Interface only**. Upload and analysis
  work, but the server blocks formal workflow-stage execution.
- `srt_full_pose` is **Interface only**; no end-to-end full-pose workflow is
  available.
- SfM CUDA is **Optional**. The default route is `pycolmap + cpu`; CUDA is
  limited to supported feature extraction and matching, and falls back to CPU
  when its capability cannot be confirmed.

The portal and viewer can keep workflow data under `--storage-root` while
serving static files from `--root`. Guided standard runs include initial route
fitting, a keyframe plan, final route fitting, quality inspection, and render
export. If CAD contains no usable road centreline, road-surface diagnostics are
recorded as skipped rather than treated as a failed render.

## Planned work

### Formal SRT/SfM integration

Connect the experimental partial-SRT core to a supported JobRunner route only
after its time synchronization, quality gates, failure handling, and manual
SfM-CAD alignment workflow have been validated. Ordinary SRT metadata will
remain supplementary evidence, not precision position, attitude, or CAD
elevation truth.

### Full-pose SRT workflow

Implement and validate an end-to-end `srt_full_pose` route. Capability
detection alone is not execution, and must not be used as an accuracy claim.

### Broader acceleration validation

Validate optional CUDA installations and quality/performance trade-offs across
supported COLMAP/pycolmap configurations. Mapping and global bundle adjustment
are not advertised as GPU processing.
