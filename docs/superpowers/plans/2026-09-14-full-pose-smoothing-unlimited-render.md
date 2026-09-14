# Full-pose smoothing and unlimited-distance rendering

> **For agentic workers:** Use executing-plans inline; preserve this existing worktree and unrelated edits.

**Goal:** Deliver a full-length 1080p30 SRT render without distance fading/culling and with continuous telemetry poses.

**Architecture:** Preserve the accepted backend projection, terrain draping, CAD colours and text. Smooth full-pose telemetry before workbench/keyframe fitting, not the rendered pixels or post-keyframe output. Keep original pose values in provenance. Default calibrated rendering has no distance limit or route-box crop; legacy non-calibrated rendering is unchanged.

**Tech Stack:** Python, NumPy, SciPy Rotation, OpenCV, pytest.

**Execution status:** Completed 2026-09-14. All three deliverables verified: 2016 tests passed / 5 skipped; independent read-only review found no blocking issue; genuine platform CLI parity passed for all 6667 poses. Full render decoded successfully at 1920×1080, 30 fps, 6667 frames. Detailed artifact and checks are recorded in `docs/full-pose-terrain-render-20260914.md`. Worktree and previous comparison artifacts retained; no merge or deletion.

## Global constraints

- Do not modify source SRT, CAD, TPKG, existing jobs or saved user keyframes.
- Keep abs_alt and the existing vertical reference; smoothing is not a datum conversion.
- No XML or SfM in this full-pose run. Keep source time/frame mapping.
- Retain invalid-data gaps and terrain coverage validation; do not invent missing terrain.
- Record new output outside project-owned storage; keep previous comparison video.

## Task 1: Unlimited calibrated visibility

- [ ] Write regression tests: projected 800 m and 80 m segments produce identical pixels; distant Chinese text survives; CAD outside the former 500 m route box remains; CLI and adapter use unlimited defaults.
- [ ] Run `python -m pytest tests/rendering/test_calibrated_overlay.py tests/cli/test_render_overlay_cli.py tests/projects/test_render_adapters.py -q` and confirm failures.
- [ ] Change calibrated max_distance_m/fade_start_m defaults to None. Apply distance tests only when explicitly configured. Remove route-box filters from calibrated preprocessing and terrain preview; retain terrain validity.
- [ ] Change calibrated CLI defaults and remove fixed 350 m adapter arguments; increment render adapter cache version.
- [ ] Re-run tests, including terrain preview regression tests.

## Task 2: Smooth full-pose telemetry before manual fitting

- [ ] Add `cadscene/srt/pose_smoothing.py` with `smooth_pose_samples(poses, time_base_seconds, window_sec=1.0)` returning copied poses and diagnostics.
- [ ] First write and run tests for staircase noise, exact constant velocity and linear height, rotation wraparound, irregular PTS, invalid gaps and input immutability.
- [ ] Implement centered local quadratic regression for XYZ and local rotation vectors around each input rotation; use source PTS, contiguous registered runs, minimum five neighbours, no extrapolation through invalid gaps.
- [ ] Integrate into `full_pose.py` after raw validation, before trajectory/path publication. Retain raw_center/raw quaternion and publish smoothing metadata; derive CSV from the same smoothed poses. Increment full-pose adapter cache version.
- [ ] Run full-pose CLI/integration and existing keyframe tests to ensure no post-fit smoothing or manual override loss.

## Task 3: Verify and render current project

- [ ] Use the real current trajectory to measure frame-to-frame jumps before/after; report them without claiming measurement accuracy.
- [ ] Build an isolated smoothed full-pose trajectory and aligned CSV, invoking the same production smoother/alignment/renderer. No project snapshot mutation.
- [ ] Run all tests; render 6667 frames at 1920x1080, 30 fps with None distance/fade limits and original CAD/TPKG.
- [ ] Decode the entire output and inspect beginning/middle/end frames; retain report, raw/smoothed poses and input paths. Deliver full video with caveat that real-world accuracy still needs user review.
