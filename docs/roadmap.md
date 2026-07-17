# Roadmap

This roadmap describes planned milestones only; no items below are part of the v0.1.0 baseline.

## v0.2.0

- COLMAP CUDA backend.

## v0.3.0

- Stage 6A upload analysis and automatic routing across `sfm_only`,
  `srt_sfm_fused`, and `srt_full_pose`.
- Video and CAD remain required; SRT is optional. Conservative metadata
  detection chooses the interface branch but is not an accuracy/truth claim.
- `sfm_only` is ready without SRT. The two SRT branches remain
  `interface_only` until their execution algorithms are delivered.

## v0.4.0

- Execute the partial-SRT/SfM fusion workflow currently exposed as an interface.

## v0.5.0

- Execute the full-pose SRT workflow currently exposed as an interface.
