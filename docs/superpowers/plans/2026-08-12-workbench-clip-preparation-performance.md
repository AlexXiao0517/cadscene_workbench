# Workbench Clip Preparation Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make on-demand workbench clip preparation fast enough to show progress almost immediately and complete the measured 57.72-second clip in roughly 15–20 seconds without weakening exact frame-boundary guarantees.

**Architecture:** Build a packet-derived `DecodedFrameIndex` only when FFprobe's declared frame count proves that packets and decoded frames are one-to-one, otherwise use the existing authoritative decoded probe. Seek near the requested interval before decoding, validate encoded output through its declared frame count when available, and select x264 `veryfast` only for workbench-triggered exports.

**Tech Stack:** Python 3, pytest, FFmpeg/FFprobe, existing project job service and clip-export CLI.

## Global Constraints

- Work only in `D:/zjic2026/cadscene_workbench/.worktrees/dxf-text-labels` on `codex/dxf-text-labels`.
- Do not alter the main worktree's Pure Rotation experiments, SOP, weekly report, or project data.
- Preserve exact half-open source PTS intervals, `clip_frame_map.json`, atomic no-clobber publication, and decoded-probe fallback.
- Keep the general CLI default preset `fast`; use `veryfast` only in workbench preparation.
- Do not merge to `main` until the user tests and approves.

---

### Task 1: Safe packet-derived frame indexing

**Files:**
- Modify: `cadscene/video_analysis/pts.py`
- Test: `tests/video_analysis/test_pts.py`

**Interfaces:**
- Consumes: existing `probe_video_pts(video_path, ffmpeg_executable=None) -> VideoPtsIndex`.
- Produces: `probe_declared_video_frame_count(video_path, ffprobe_executable=None) -> int | None` and `probe_fast_frame_index(video_path, ffprobe_executable=None, ffmpeg_executable=None) -> DecodedFrameIndex | None`.

- [ ] **Step 1: Write failing metadata and fast-index tests**

Add tests that stub FFprobe JSON as `{"streams":[{"nb_frames":"3"}]}`, expect the integer `3`, expect `None` for `N/A`, and provide three ordered packets whose count and durations produce a `DecodedFrameIndex`. Add rejection cases for count mismatch, duplicate PTS, and non-integral/non-positive durations.

```python
index = pts.probe_fast_frame_index(video, ffprobe_executable=ffprobe)
assert [frame.pts for frame in index.frames] == [5000, 5040, 5080]
assert all(frame.timestamp_source == "packet_pts" for frame in index.frames)
```

- [ ] **Step 2: Run tests and observe the missing-API failure**

Run: `python -m pytest tests/video_analysis/test_pts.py -q`

Expected: failure because `probe_declared_video_frame_count` and `probe_fast_frame_index` do not exist.

- [ ] **Step 3: Implement the minimal safe probes**

Resolve FFprobe with the existing resolver, call `-select_streams v:0 -show_entries stream=nb_frames -of json`, return `None` for unavailable/non-positive declarations, then convert packet PTS and duration seconds back into exact time-base units only after all safety checks pass. Catch metadata/packet-probe failures in `probe_fast_frame_index` and return `None` so callers can use the authoritative path.

```python
def probe_fast_frame_index(...) -> DecodedFrameIndex | None:
    declared_count = probe_declared_video_frame_count(...)
    if declared_count is None:
        return None
    packet_index = probe_video_pts(...)
    if len(packet_index.packets) != declared_count:
        return None
    # validate unique PTS and positive exact durations, then construct frames
```

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/video_analysis/test_pts.py -q`

Expected: all tests in the module pass.

### Task 2: Fast seek and fast output validation

**Files:**
- Modify: `cadscene/video_analysis/clip_export.py`
- Test: `tests/video_analysis/test_clip_export.py`

**Interfaces:**
- Consumes: Task 1's `probe_fast_frame_index` and `probe_declared_video_frame_count`.
- Produces: export selection/fallback behavior and `_build_ffmpeg_clip_command` with input-side pre-roll.

- [ ] **Step 1: Write failing export-path tests**

Add one test proving the fast source index avoids `probe_decoded_frame_index`, one proving a `None` fast index invokes it, one proving declared output count avoids decoded output probing, and one proving unavailable output metadata falls back. Update the command test to require `-ss` before `-copyts` and `-i` while the absolute `trim=start_pts=...:end_pts=...` remains present.

```python
assert command.index("-ss") < command.index("-copyts") < command.index("-i")
assert command[command.index("-ss") + 1] == "0"
```

For a clip starting at 115.44 seconds, assert the pre-roll seek is `105.44`.

- [ ] **Step 2: Run tests and observe the old slow-path assertions fail**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: failures because export always decodes source/output indexes and the command has no `-ss`.

- [ ] **Step 3: Implement minimal selection and pre-roll**

Use `probe_fast_frame_index(...) or probe_decoded_frame_index(...)` for the source. For output, use declared count when present and decode only when it is absent. Insert `-ss`, formatted from `max(0.0, clip.start_pts_sec - 10.0)`, before `-copyts -i`, retaining the existing filters and progress calculation.

```python
fast_index = probe_fast_frame_index(source, ffmpeg_executable=ffmpeg)
frame_index = fast_index or probe_decoded_frame_index(source, ffmpeg_executable=ffmpeg)
declared_count = probe_declared_video_frame_count(clip_path)
actual_count = declared_count if declared_count is not None else len(
    probe_decoded_frame_index(clip_path, ffmpeg_executable=ffmpeg).frames
)
```

- [ ] **Step 4: Run export tests**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: all tests in the module pass, including real nonzero-PTS exports.

### Task 3: Select the interactive encoding preset

**Files:**
- Modify: `cadscene/projects/service.py`
- Test: `tests/projects/test_executor.py`

**Interfaces:**
- Consumes: existing `cadscene.cli.export_video_clips --preset` option.
- Produces: project workbench clip-export commands containing `--preset veryfast`.

- [ ] **Step 1: Write the failing service-plan assertion**

Extend `test_service_builds_existing_clip_export_cli_plan_inside_attempt`:

```python
assert command[command.index("--preset") + 1] == "veryfast"
```

- [ ] **Step 2: Run the service test and observe failure**

Run: `python -m pytest tests/projects/test_executor.py::test_service_builds_existing_clip_export_cli_plan_inside_attempt -q`

Expected: failure because `--preset` is absent.

- [ ] **Step 3: Add the explicit preset to the generated command**

Insert `"--preset", "veryfast"` into `_prepare_clip_export` without changing CLI defaults.

- [ ] **Step 4: Run focused service tests**

Run: `python -m pytest tests/projects/test_executor.py::test_service_builds_existing_clip_export_cli_plan_inside_attempt tests/video_analysis/test_cli.py -q`

Expected: all selected tests pass.

### Task 4: Regression and real-project verification

**Files:**
- Verify only: source and test files from Tasks 1–3.

**Interfaces:**
- Consumes: completed optimization.
- Produces: fresh automated and measured evidence; no persistent benchmark artifact.

- [ ] **Step 1: Run the focused regression set**

Run: `python -m pytest tests/video_analysis/test_pts.py tests/video_analysis/test_clip_export.py tests/video_analysis/test_cli.py tests/projects/test_executor.py -q`

Expected: zero failures.

- [ ] **Step 2: Run the full suite**

Run: `python -m pytest -q`

Expected: zero failures, with only the repository's known skips/warning.

- [ ] **Step 3: Benchmark the reported clip**

Run the updated CLI against clip `clip-0003` from project `dataset-61854a41-3726-4b46-af6f-eef0e616eb06` into a uniquely named directory under the Windows temporary directory, with `--preset veryfast --allow-subset`. Record elapsed time and progress timestamps.

- [ ] **Step 4: Verify exact boundaries and remove only the benchmark directory**

Use FFprobe to assert `nb_frames == 1443` and container duration about `57.8` seconds. Resolve the benchmark directory and verify it is beneath the Windows temporary root before removing that exact directory.

- [ ] **Step 5: Restart the feature service**

Verify port 8300 belongs to the previously recorded feature-service PID before stopping it. Start the service from the isolated worktree with the main repository as storage root, then verify the viewer returns HTTP 200. Do not merge or push.
