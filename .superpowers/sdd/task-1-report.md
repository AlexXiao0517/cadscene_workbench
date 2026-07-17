# Task 1 — SRT parser and trajectory capability detection

## Changed files

- `cadscene/srt/__init__.py` — public `analyze_srt_stream` and
  `detect_trajectory_capability` exports.
- `cadscene/srt/schema.py` — dependency-free `SrtRecord` schema.
- `cadscene/srt/parser.py` — incremental SRT block parsing, aliases, and the
  unified analysis entry point.
- `cadscene/srt/capability.py` — coverage statistics, warnings, and
  conservative mode classification.
- `tests/srt/__init__.py` and `tests/srt/test_capability.py` — six behavior
  tests.
- `tests/fixtures/srt/*.srt` — five minimal synthetic fixtures only.

## RED / GREEN evidence

### RED

Command:

```text
python -m pytest tests/srt/test_capability.py -v
```

Output before production implementation:

```text
ModuleNotFoundError: No module named 'cadscene.srt'
```

This is the expected missing-feature failure.

### GREEN

Command:

```text
python -m pytest tests/srt/test_capability.py -v
```

Output:

```text
6 passed in 0.02s
```

Final broader verification:

```text
python -m pytest tests -q
python -m compileall -q cadscene/srt
git diff --check
```

Output:

```text
257 passed, 1 warning in 28.33s
```

`compileall` and `git diff --check` exited zero. The lone test warning is an
existing `fontTools` deprecation warning.

## Conservative classification rules

- `srt_full_pose` requires at least two records, at least 80% latitude,
  longitude, and altitude coverage, and at least 80% coverage for all three
  gimbal attitude axes.
- Drone/aircraft attitude is tracked separately. It can never populate the
  public camera yaw/pitch/roll fields or yield `srt_full_pose`.
- Missing/insufficient GPS or altitude yields `sfm_only` and a warning.
- SRT/video duration disagreement adds a warning but does not silently change
  a supported trajectory mode.

## Self-review

- Confirmed only the new SRT package, its tests, its synthetic fixtures, and
  this report are staged for this task.
- No SfM, alignment, quality, viewer-scene, road-diagnostic, rendering, real
  telemetry, `project/`, `data/`, or `runs/` files were changed.
- No runtime dependencies were added.
- Parser tolerates UTF-8 decode errors and malformed blocks by returning no
  records and an explicit parse warning rather than raising.

## Concerns

- A root-level `python -m pytest -q` also collects legacy `project/tests`,
  which fail collection because their separate `cadvideo` package is not on
  this environment's import path (14 `ModuleNotFoundError` errors). The
  maintained `tests/` suite used for this change passes fully.
- The parser intentionally recognizes common key/value DJI SRT forms and
  textual aliases, not every vendor-specific free-text telemetry dialect.

## Commit

`b70dca0` — `Add conservative SRT capability detection`

## Review-fix addendum

### Findings addressed

1. Full-pose classification now requires at least 80% *complete same-record*
   observations. Each qualifying observation contains finite latitude,
   longitude, altitude, gimbal yaw, gimbal pitch, and gimbal roll. Marginal
   per-field coverage alone cannot yield `srt_full_pose`.
2. DJI bracket aliases `rel_alt`, `abs_alt`, `gb_yaw`, `gb_pitch`, and
   `gb_roll` are normalized to the existing altitude/gimbal schema fields.

### RED

After adding two regression tests, the focused command failed as expected:

```text
python -m pytest tests/srt/test_capability.py -v
2 failed, 6 passed
```

The failures showed `srt_full_pose` for an input with only 40% complete pose
records and `sfm_only` for the short DJI aliases.

### GREEN

```text
python -m pytest tests/srt/test_capability.py -v
8 passed in 0.02s
git diff --check
```

Both commands exited zero. This addendum changed only the SRT classifier,
parser alias map, synthetic alias fixture, focused tests, and this report.
