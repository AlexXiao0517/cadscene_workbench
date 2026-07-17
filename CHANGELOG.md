# Changelog

## Unreleased

- Add the Stage 6A upload portal and automatic routing documentation.
- Document required video/CAD inputs, optional SRT input, and the three modes:
  `sfm_only` is ready; `srt_sfm_fused` and `srt_full_pose` are interface-only.
- Document conservative SRT capability detection. It is a metadata capability
  label, not a high-precision trajectory or pose truth claim.

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
