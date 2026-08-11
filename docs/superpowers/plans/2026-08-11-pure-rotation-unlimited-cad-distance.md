# Pure Rotation Unlimited CAD Distance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the 900 metre far-distance cutoff only from Pure Rotation CAD rendering while preserving perspective, near-plane clipping, and all SfM behavior.

**Architecture:** Extend the shared overlay renderer with an explicit `None` maximum-distance state while retaining its 900 metre default. Expose that state through a dedicated Pure Rotation CLI flag, and have only the Pure Rotation project render adapter pass the flag.

**Tech Stack:** Python 3.11, NumPy, OpenCV, argparse, pytest.

## Global Constraints

- Only `pure_rotation` project rendering disables the far-distance cutoff.
- Near-plane clipping, camera frustum, perspective projection, and image-boundary clipping remain active.
- Existing SfM and CLI callers retain the 900 metre default.
- `render_stats.json` records `max_distance_m` as JSON `null` when disabled.
- Do not modify trajectory recovery, camera calibration, or workbench adjustment behavior.

---

### Task 1: Shared overlay unlimited-distance contract

**Files:**
- Modify: `tests/rendering/test_overlay.py`
- Modify: `cadscene/rendering/overlay.py`

**Interfaces:**
- Consumes: `render_frame_overlay(..., max_distance_m: float | None)` and `RenderOverlayConfig.max_distance_m`.
- Produces: `None` means no far-distance clipping; numeric values retain current clipping and fading.

- [ ] **Step 1: Write the failing projection test**

```python
def test_none_max_distance_keeps_far_lines_visible() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    cad = _bundle([RoadLine(points=np.asarray([[0.0, 1000.0], [10.0, 1000.0]]), kind="center")])

    out = render_frame_overlay(image, _camera(), cad, max_distance_m=None)

    assert int(out.sum()) > 0
```

- [ ] **Step 2: Run the test and verify RED**

Run: `D:\anaconda3\python.exe -m pytest tests/rendering/test_overlay.py::test_none_max_distance_keeps_far_lines_visible -q`

Expected: FAIL because `float(None)` is not supported.

- [ ] **Step 3: Implement optional far-distance clipping**

Change `RenderOverlayConfig.max_distance_m` and `render_frame_overlay` to accept `float | None`. Compute `max_depth` only for numeric values, use `depth > near_plane_m` when it is `None`, and only calculate fading when a finite maximum exists. Serialize the raw optional value into stats.

- [ ] **Step 4: Run focused overlay tests and verify GREEN**

Run: `D:\anaconda3\python.exe -m pytest tests/rendering/test_overlay.py -q`

Expected: PASS, including the existing numeric 900 metre cutoff tests.

### Task 2: Pure Rotation CLI and adapter opt-in

**Files:**
- Modify: `tests/pure_rotation/test_rendering.py`
- Modify: `tests/projects/test_render_adapters.py`
- Modify: `cadscene/cli/render_pure_rotation.py`
- Modify: `cadscene/projects/workbench_render_adapter.py`

**Interfaces:**
- Produces: `render_pure_rotation --no-distance-limit` maps to `RenderOverlayConfig.max_distance_m=None`.
- Consumes: `ExistingWorkbenchRenderAdapter.prepare()` adds the flag only for `workflow == "pure_rotation"`.

- [ ] **Step 1: Write failing CLI and adapter contract tests**

Add assertions that CLI help contains `--no-distance-limit`, the Pure Rotation render command contains that flag and omits `--max-distance-m`, while an SfM render command contains neither Pure Rotation flag.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `D:\anaconda3\python.exe -m pytest tests/pure_rotation/test_rendering.py tests/projects/test_render_adapters.py::test_pure_rotation_render_uses_immutable_workbench_track_and_attempt_output -q`

Expected: FAIL because the new flag is absent.

- [ ] **Step 3: Implement the explicit CLI flag and adapter wiring**

Place `--max-distance-m` and `--no-distance-limit` in one argparse mutually exclusive group, retain `900.0` as the maximum-distance default, and pass `None if args.no_distance_limit else args.max_distance_m` into the config. Replace the adapter's `--max-distance-m 900` arguments with `--no-distance-limit`.

- [ ] **Step 4: Run focused rendering and adapter tests and verify GREEN**

Run: `D:\anaconda3\python.exe -m pytest tests/rendering/test_overlay.py tests/pure_rotation/test_rendering.py tests/projects/test_render_adapters.py -q`

Expected: PASS.

### Task 3: Real-output and regression verification

**Files:**
- No production file changes.

**Interfaces:**
- Consumes: the latest project Pure Rotation attempt inputs.
- Produces: a newly rendered diagnostic output whose stats contain `max_distance_m: null`.

- [ ] **Step 1: Re-run the latest real Pure Rotation render command into a new diagnostic directory**

Use the latest project job's immutable video, CAD, track, scale, and origin with `--no-distance-limit`; do not overwrite the published render revision.

- [ ] **Step 2: Inspect representative frames**

Build a contact sheet from the diagnostic MP4 and verify distant CAD that is inside the current camera view is retained. Confirm perspective and near-plane behavior remain unchanged.

- [ ] **Step 3: Run related and full tests**

Run: `D:\anaconda3\python.exe -m pytest tests/rendering tests/pure_rotation tests/projects/test_render_adapters.py -q`

Run: `D:\anaconda3\python.exe -m pytest -q`

Expected: all tests pass; only the existing documented skips and warnings remain.

- [ ] **Step 4: Commit implementation**

```powershell
git add cadscene/rendering/overlay.py cadscene/cli/render_pure_rotation.py cadscene/projects/workbench_render_adapter.py tests/rendering/test_overlay.py tests/pure_rotation/test_rendering.py tests/projects/test_render_adapters.py
git commit -m "fix: show all distant CAD in rotation renders"
```
