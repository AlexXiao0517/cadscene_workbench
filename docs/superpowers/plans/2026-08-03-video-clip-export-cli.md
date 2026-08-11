# Video Clip Export CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an explicitly invoked Python CLI that exports physical preview MP4 files from a Stage 8A logical clip manifest.

**Architecture:** A focused `clip_export` module validates manifest ranges and orchestrates FFmpeg into a sibling temporary directory before atomic publication. A thin `cadscene.cli.export_video_clips` module parses arguments and prints the published result; analysis and workflow modules remain unchanged.

**Tech Stack:** Python 3.10+, argparse, dataclasses, pathlib, subprocess, tempfile, FFmpeg resolved through `imageio-ffmpeg`, pytest.

## Global Constraints

- Use `source_start_pts_sec` and `source_end_pts_sec` directly; never derive frame numbers from FPS.
- Re-encode H.264 previews instead of stream-copying so keyframes do not displace boundaries.
- Every manifest interval must be positive, ordered, non-overlapping, finite, and strictly shorter than 60 seconds.
- Never overwrite an existing output directory and never publish partial output.
- Do not modify or invoke video analysis, workflow routing, SfM, SRT, or Pure Rotation code.

---

### Task 1: Manifest contracts

**Files:**
- Create: `cadscene/video_analysis/clip_export.py`
- Create: `tests/video_analysis/test_clip_export.py`

**Interfaces:**
- Produces: `ExportClip(clip_id: str, start_pts_sec: float, end_pts_sec: float)`.
- Produces: `load_export_clips(manifest_path: Path) -> list[ExportClip]`.

- [ ] **Step 1: Write failing contract tests**

Test a valid two-clip manifest and rejection of empty clips, unsafe IDs such as
`../escape`, non-finite values, non-positive ranges, overlaps, and durations
greater than or equal to 60 seconds.

```python
clips = load_export_clips(manifest)
assert [(item.clip_id, item.duration_sec) for item in clips] == [
    ("clip-0001", 2.0),
    ("clip-0002", 2.5),
]
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: collection fails because `cadscene.video_analysis.clip_export` does not exist.

- [ ] **Step 3: Implement minimal validation**

Use a frozen dataclass, JSON object/list checks, `math.isfinite`, a conservative
`[A-Za-z0-9][A-Za-z0-9._-]{0,127}` ID regex, and previous-end comparison.

```python
@dataclass(frozen=True)
class ExportClip:
    clip_id: str
    start_pts_sec: float
    end_pts_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_pts_sec - self.start_pts_sec
```

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: all manifest contract tests pass.

- [ ] **Step 5: Commit**

```text
git add cadscene/video_analysis/clip_export.py tests/video_analysis/test_clip_export.py
git commit -m "feat: validate logical clip export manifests"
```

### Task 2: Atomic FFmpeg export

**Files:**
- Modify: `cadscene/video_analysis/clip_export.py`
- Modify: `tests/video_analysis/test_clip_export.py`

**Interfaces:**
- Consumes: `load_export_clips()` and the existing `resolve_ffmpeg_executable()`.
- Produces: `export_video_clips(video_path, manifest_path, output_dir, *, ffmpeg_executable=None, preset="fast", crf=18) -> list[Path]`.

- [ ] **Step 1: Write failing integration tests**

Create a four-second FFV1 test video with FFmpeg, export ranges `0-2` and `2-4`,
then probe both output MP4 files and assert their durations are within 0.25 seconds
of two seconds. Add tests that an existing output directory is refused and a
corrupt source does not leave either the final directory or sibling temp output.

```python
paths = export_video_clips(video, manifest, output)
assert [path.name for path in paths] == ["clip-0001.mp4", "clip-0002.mp4"]
assert all(abs(probe_duration(path) - 2.0) < 0.25 for path in paths)
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: fails because `export_video_clips` is undefined.

- [ ] **Step 3: Implement minimal exporter**

Validate paths/options, resolve FFmpeg, create a same-parent temporary directory,
run one checked subprocess per clip with accurate input seeking and H.264/AAC
encoding, verify every output is non-empty, then `os.replace(temp_dir, output_dir)`.
Always remove the temporary directory in `finally` if publication did not occur.

```text
ffmpeg -ss START -i VIDEO -t DURATION -map 0:v:0 -map 0:a? \
  -c:v libx264 -preset PRESET -crf CRF -pix_fmt yuv420p \
  -c:a aac -b:a 192k -movflags +faststart -avoid_negative_ts make_zero OUTPUT
```

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/video_analysis/test_clip_export.py -q`

Expected: contract and real FFmpeg integration tests pass.

- [ ] **Step 5: Commit**

```text
git add cadscene/video_analysis/clip_export.py tests/video_analysis/test_clip_export.py
git commit -m "feat: atomically export logical video clips"
```

### Task 3: CLI and real-video acceptance

**Files:**
- Create: `cadscene/cli/export_video_clips.py`
- Modify: `tests/video_analysis/test_cli.py`

**Interfaces:**
- Consumes: `export_video_clips()`.
- Produces module invocation `python -m cadscene.cli.export_video_clips` with
  `--video`, `--manifest`, `--output-dir`, `--ffmpeg`, `--preset`, and `--crf`.

- [ ] **Step 1: Write failing CLI help test**

```python
result = subprocess.run(
    [sys.executable, "-m", "cadscene.cli.export_video_clips", "--help"],
    capture_output=True,
    text=True,
)
assert result.returncode == 0
assert "--manifest" in result.stdout
assert "--output-dir" in result.stdout
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/video_analysis/test_cli.py -q`

Expected: the new module cannot be imported.

- [ ] **Step 3: Implement the thin CLI**

Parse typed paths, restrict preset choices, validate CRF through the core
function, invoke the exporter, and print the resolved output directory plus
`Exported N clips`.

- [ ] **Step 4: Run focused and full automated verification**

Run:

```text
python -m pytest tests/video_analysis -q
python -m pytest -q
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 5: Export and inspect `jinhuaorigin.mp4`**

Run the new CLI against
`work/stage8a_validation/jinhua-reviewed-run/02_video_analysis/clip_manifest.json`
into a new ignored preview directory. Verify exactly 11 non-empty MP4 files and
probe each duration against its manifest interval within 0.25 seconds.

- [ ] **Step 6: Commit**

```text
git add cadscene/cli/export_video_clips.py tests/video_analysis/test_cli.py
git commit -m "feat: add standalone video clip export CLI"
```
