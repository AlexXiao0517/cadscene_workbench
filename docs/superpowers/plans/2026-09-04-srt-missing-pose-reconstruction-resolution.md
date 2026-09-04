# Missing-Attitude SRT Reconstruction Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conditional 720p/1080p/source COLMAP solve-resolution setting to `srt_fixed_track_visual_pose`, resize frames before writing them, preserve the original-resolution preview/render path, and expose the actual solve parameters in diagnostics.

**Architecture:** Keep the user-facing enum in project settings and resolve it through one dependency-light `cadscene.sfm.resolution` policy module. The workflow adapter computes the target image size and matching PINHOLE intrinsics, while `run_sfm` performs the physical resize during frame extraction and exports trajectories using the actual solve dimensions. The fixed-track builder keeps source-video intrinsics for final rendering and carries the separate solve dimensions into reports and the workbench scene.

**Tech Stack:** Python 3.11+, dataclasses, OpenCV, NumPy, COLMAP CLI, pytest, browser-native HTML/JavaScript.

## Global Constraints

- The setting is visible only for `srt_fixed_track_visual_pose`; `srt_full_pose` does not show it and never starts COLMAP.
- Allowed stored values are exactly `720p`, `1080p`, and `source`; a missing legacy value is interpreted as `1080p`.
- `1080p` is the recommended and persisted default; `720p` is the fast option; `source` uses the source dimensions.
- Resizing preserves aspect ratio, never upscales a smaller source, and rounds resized dimensions down to positive even integers.
- The integer horizontal FOV remains unchanged; PINHOLE `fx/fy/cx/cy` are recomputed from the actual solve width and height.
- Camera centers remain strictly SRT→CAD; COLMAP supplies only orientation and the optional diagnostic sparse cloud.
- Preview, six-degree-of-freedom keyframe refinement, route fitting, save, and final render continue to use the original video dimensions.
- Missing-attitude jobs request `device=auto`; CUDA is used only when verified by the existing backend selection and otherwise falls back to CPU with diagnostics.
- Do not introduce an OpenCV attitude-estimation fallback.
- Preserve all pre-existing unstaged changes. In particular, stage only the new hunks in `cadscene/projects/service.py` and `apps/project_workspace/project_workspace.js`.

## File Map

- Create `cadscene/sfm/resolution.py`: enum normalization and deterministic source-to-solve dimension calculation.
- Create `tests/sfm/test_resolution.py`: unit contract for defaults, valid values, invalid values, aspect ratio, no-upscale, and even dimensions.
- Modify `cadscene/sfm/reconstruction.py`: resize extracted frames, use solve dimensions during export, and record source/solve diagnostics.
- Modify `cadscene/cli/run_sfm.py`: accept `--reconstruction-height` and record it in stage inputs.
- Modify `tests/sfm/test_reconstruction.py` and `tests/cli/test_run_sfm_backend_cli.py`: frame-resize and CLI/stats contracts.
- Modify `cadscene/projects/workflow_adapters.py`: normalize the setting, compute solve intrinsics, request auto device, and bump the fixed-track adapter version.
- Modify `cadscene/srt/fixed_track_visual_pose.py`: serialize the chosen resolution with the fixed-track build configuration.
- Modify `cadscene/cli/build_srt_fixed_track_visual_pose.py`: publish distinct source-video and reconstruction sizes in final artifacts.
- Modify `tests/projects/test_workflow_adapters.py`, `tests/srt/test_fixed_track_visual_pose.py`, and `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`: adapter and artifact regression tests.
- Modify `cadscene/projects/service.py` and `cadscene/projects/http_api.py`: validate, persist, restore, and fingerprint the setting.
- Modify `tests/projects/test_fixed_track_visual_pose_configuration.py`: service/API/default/backward-compatibility tests.
- Modify `apps/project_workspace/index.html` and `apps/project_workspace/project_workspace.js`: conditional selector and request payload.
- Modify `tests/viewer/test_project_workspace_static.py`: conditional frontend contract.
- Modify `apps/web_camera_viewer/viewer_legacy.js` and `apps/web_camera_viewer/index.html`: show actual solve parameters and refresh browser assets.
- Modify `tests/viewer/test_workflow_ui_static.py`: workbench diagnostic contract.

---

### Task 1: Centralize the Reconstruction Resolution Policy

**Files:**
- Create: `cadscene/sfm/resolution.py`
- Create: `tests/sfm/test_resolution.py`

**Interfaces:**
- Consumes: source `width: int`, source `height: int`, and a stored `value: object`.
- Produces: `normalize_reconstruction_resolution(value: object = None) -> str`, `target_height_for_resolution(value: object = None) -> int | None`, and `reconstruction_dimensions(width: int, height: int, value: object = None) -> tuple[int, int]`.

- [ ] **Step 1: Write the failing policy tests**

```python
import pytest

from cadscene.sfm.resolution import (
    normalize_reconstruction_resolution,
    reconstruction_dimensions,
    target_height_for_resolution,
)


def test_missing_resolution_defaults_to_1080p() -> None:
    assert normalize_reconstruction_resolution(None) == "1080p"
    assert target_height_for_resolution(None) == 1080


@pytest.mark.parametrize(
    ("value", "expected"),
    [("720p", 720), ("1080p", 1080), ("source", None)],
)
def test_supported_resolutions_map_to_target_heights(value, expected) -> None:
    assert normalize_reconstruction_resolution(value) == value
    assert target_height_for_resolution(value) == expected


@pytest.mark.parametrize("value", ["", "4k", "2160p", 1080, True])
def test_invalid_resolution_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="reconstruction_resolution"):
        normalize_reconstruction_resolution(value)


def test_4k_source_scales_to_requested_even_dimensions() -> None:
    assert reconstruction_dimensions(3840, 2160, "1080p") == (1920, 1080)
    assert reconstruction_dimensions(3840, 2160, "720p") == (1280, 720)
    assert reconstruction_dimensions(4096, 2160, "1080p") == (2048, 1080)


def test_small_source_is_not_upscaled_and_source_is_unchanged() -> None:
    assert reconstruction_dimensions(1280, 720, "1080p") == (1280, 720)
    assert reconstruction_dimensions(3840, 2160, "source") == (3840, 2160)


def test_scaled_width_is_rounded_down_to_an_even_integer() -> None:
    width, height = reconstruction_dimensions(4001, 2160, "720p")
    assert (width, height) == (1332, 720)
    assert width % 2 == 0 and height % 2 == 0


@pytest.mark.parametrize(("width", "height"), [(0, 1080), (1920, 0), (-1, 720)])
def test_invalid_source_dimensions_are_rejected(width, height) -> None:
    with pytest.raises(ValueError, match="source video dimensions"):
        reconstruction_dimensions(width, height, "1080p")
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `python -m pytest tests/sfm/test_resolution.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'cadscene.sfm.resolution'`.

- [ ] **Step 3: Implement the policy module**

```python
from __future__ import annotations


SUPPORTED_RECONSTRUCTION_RESOLUTIONS = ("720p", "1080p", "source")
_TARGET_HEIGHTS: dict[str, int | None] = {
    "720p": 720,
    "1080p": 1080,
    "source": None,
}


def normalize_reconstruction_resolution(value: object = None) -> str:
    normalized = "1080p" if value is None else value
    if not isinstance(normalized, str) or normalized not in _TARGET_HEIGHTS:
        allowed = ", ".join(SUPPORTED_RECONSTRUCTION_RESOLUTIONS)
        raise ValueError(
            f"reconstruction_resolution must be one of: {allowed}"
        )
    return normalized


def target_height_for_resolution(value: object = None) -> int | None:
    return _TARGET_HEIGHTS[normalize_reconstruction_resolution(value)]


def reconstruction_dimensions(
    width: int,
    height: int,
    value: object = None,
) -> tuple[int, int]:
    source_width = int(width)
    source_height = int(height)
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source video dimensions must be positive")
    target_height = target_height_for_resolution(value)
    if target_height is None or source_height <= target_height:
        return source_width, source_height
    scaled_width = int(source_width * (target_height / source_height))
    scaled_width -= scaled_width % 2
    scaled_height = target_height - target_height % 2
    return max(2, scaled_width), max(2, scaled_height)
```

- [ ] **Step 4: Run the policy tests**

Run: `python -m pytest tests/sfm/test_resolution.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the isolated policy**

```bash
git add cadscene/sfm/resolution.py tests/sfm/test_resolution.py
git commit -m "feat: define SRT reconstruction resolution policy"
```

---

### Task 2: Resize Frames Before COLMAP and Record the Actual Dimensions

**Files:**
- Modify: `cadscene/sfm/reconstruction.py`
- Modify: `cadscene/cli/run_sfm.py`
- Modify: `tests/sfm/test_reconstruction.py`
- Modify: `tests/cli/test_run_sfm_backend_cli.py`

**Interfaces:**
- Consumes: `ReconstructionConfig.reconstruction_height: int | None` and `extract_frames(..., output_size: tuple[int, int] | None)`.
- Produces: PNG frames at the requested solve size, trajectory/intrinsics tagged with that size, and `sfm_stats.json` fields `source_image_size`, `reconstruction_image_size`, and `camera_params`.

- [ ] **Step 1: Add failing extraction, stats, and CLI tests**

Append to `tests/sfm/test_reconstruction.py`:

```python
def test_extract_frames_resizes_before_writing_without_changing_frame_indices(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "tiny.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48)
    )
    for value in range(4):
        writer.write(np.full((48, 64, 3), value * 20, dtype=np.uint8))
    writer.release()

    from cadscene.sfm.reconstruction import extract_frames

    extracted = extract_frames(
        video, tmp_path / "images", [1, 3], output_size=(32, 24)
    )

    assert [item.frame_index for item in extracted] == [1, 3]
    assert cv2.imread(str(extracted[0].path)).shape[:2] == (24, 32)


def test_stats_record_source_and_actual_reconstruction_dimensions() -> None:
    config = ReconstructionConfig(
        reconstruction_height=1080,
        camera_model="PINHOLE",
        camera_params=(1321.32664365, 1321.32664365, 960.0, 540.0),
    )
    stats = build_sfm_stats(
        frame_count=100,
        extracted_frame_count=20,
        registered_count=18,
        point_count=5000,
        mean_reprojection_error=0.5,
        config=config,
        backend="colmap_cli",
        runtime={
            "source_image_size": [3840, 2160],
            "reconstruction_image_size": [1920, 1080],
        },
    )

    assert stats["source_image_size"] == [3840, 2160]
    assert stats["reconstruction_image_size"] == [1920, 1080]
    assert stats["camera_params"] == [1321.32664365, 1321.32664365, 960.0, 540.0]
```

Append to `tests/cli/test_run_sfm_backend_cli.py`:

```python
def test_run_sfm_accepts_reconstruction_height() -> None:
    from cadscene.cli.run_sfm import build_parser

    args = build_parser().parse_args(
        [
            "--dataset", "demo",
            "--run-id", "resolution",
            "--reconstruction-height", "1080",
        ]
    )

    assert args.reconstruction_height == 1080
```

Update the existing `test_colmap_cli_cpu_retry_preserves_all_ba_kwargs` extractor double so the new keyword remains observable:

```python
    extracted_sizes = []

    def fake_extract_frames(
        video_path, images_dir, frame_indices, *, output_size=None
    ):
        extracted_sizes.append(output_size)
        return [
            reconstruction.ExtractedFrame(
                frame_index=index,
                path=images_dir / f"frame_{index:06d}.png",
            )
            for index in frame_indices
        ]

    monkeypatch.setattr(reconstruction, "extract_frames", fake_extract_frames)
```

Run that test with `reconstruction_height=720` and append:

```python
    assert extracted_sizes == [(64, 48)]
```

This verifies that a source smaller than 720p is not upscaled while preserving the existing CUDA-to-CPU retry contract.

- [ ] **Step 2: Run the focused tests and verify the new interfaces fail**

Run: `python -m pytest tests/sfm/test_reconstruction.py::test_extract_frames_resizes_before_writing_without_changing_frame_indices tests/sfm/test_reconstruction.py::test_stats_record_source_and_actual_reconstruction_dimensions tests/cli/test_run_sfm_backend_cli.py::test_run_sfm_accepts_reconstruction_height -q`

Expected: failures mention the unexpected `output_size`, unexpected `reconstruction_height`, and unrecognized CLI argument.

- [ ] **Step 3: Add the reconstruction config and physical resize**

Add the field to `ReconstructionConfig`:

```python
    reconstruction_height: int | None = None
```

Change the extractor signature and write path:

```python
def extract_frames(
    video_path: str | Path,
    images_dir: str | Path,
    frame_indices: Sequence[int],
    *,
    output_size: tuple[int, int] | None = None,
) -> list[ExtractedFrame]:
```

```python
            if frame_index in target_set:
                path = output / frame_image_name(frame_index)
                output_frame = frame
                if output_size is not None:
                    target_width, target_height = output_size
                    if target_width <= 0 or target_height <= 0:
                        raise ValueError("output_size must contain positive dimensions")
                    if frame.shape[1] != target_width or frame.shape[0] != target_height:
                        output_frame = cv2.resize(
                            frame,
                            (target_width, target_height),
                            interpolation=cv2.INTER_AREA,
                        )
                if output_frame.size == 0 or not cv2.imwrite(str(path), output_frame):
                    raise RuntimeError(f"failed to write extracted frame: {path}")
```

- [ ] **Step 4: Use solve dimensions consistently in `run_reconstruction`**

Import the policy helper:

```python
from cadscene.sfm.resolution import reconstruction_dimensions
```

After `_video_metadata(video)`, calculate the actual size without inventing a new enum:

```python
    frame_count, fps, width, height = _video_metadata(video)
    if config.reconstruction_height is None:
        solve_width, solve_height = width, height
    else:
        solve_width, solve_height = reconstruction_dimensions(
            width, height, f"{int(config.reconstruction_height)}p"
        )
```

Validate the CLI-facing field before this calculation:

```python
    if config.reconstruction_height not in (None, 720, 1080):
        raise ValueError("reconstruction_height must be 720, 1080, or omitted")
```

Pass the actual dimensions to extraction and every `_export_from_reconstruction` / `export_colmap_text_model` call in the non-default path:

```python
    notify(
        "extract_frames",
        0.12,
        f"正在从视频抽帧（{width}×{height} → {solve_width}×{solve_height}）",
    )
    frames = extract_frames(
        video,
        images_dir,
        frame_indices,
        output_size=(solve_width, solve_height),
    )
```

```python
                width=solve_width,
                height=solve_height,
```

Seed both runtime dictionaries with the same diagnostic values:

```python
        "source_image_size": [width, height],
        "reconstruction_image_size": [solve_width, solve_height],
```

Extend `build_sfm_stats` without changing existing keys:

```python
        "source_image_size": runtime_values.get("source_image_size"),
        "reconstruction_image_size": runtime_values.get(
            "reconstruction_image_size"
        ),
        "camera_params": (
            list(config.camera_params) if config.camera_params is not None else None
        ),
```

Add report lines after the device lines:

```python
        f"- 源视频尺寸：{stats.get('source_image_size') or '未记录'}",
        f"- 姿态解算尺寸：{stats.get('reconstruction_image_size') or '未记录'}",
        f"- 相机模型/参数：{stats.get('camera_model')} / {stats.get('camera_params') or '自动'}",
```

- [ ] **Step 5: Wire `--reconstruction-height` through the CLI and manifest**

Add to `build_parser()`:

```python
    parser.add_argument(
        "--reconstruction-height",
        type=int,
        choices=(720, 1080),
        default=None,
        help="Resize extracted frames to this height before COLMAP; smaller sources are not upscaled.",
    )
```

Pass it into the dataclass and inputs map:

```python
        reconstruction_height=args.reconstruction_height,
```

```python
        "reconstruction_height": args.reconstruction_height,
```

- [ ] **Step 6: Run reconstruction and CLI tests**

Run: `python -m pytest tests/sfm/test_resolution.py tests/sfm/test_reconstruction.py tests/cli/test_run_sfm_cli.py tests/cli/test_run_sfm_backend_cli.py -q`

Expected: all tests pass; the existing default CLI assertions remain `pycolmap` and `cpu` because only the fixed-track adapter changes that default.

- [ ] **Step 7: Commit the extraction boundary**

```bash
git add cadscene/sfm/reconstruction.py cadscene/cli/run_sfm.py tests/sfm/test_reconstruction.py tests/cli/test_run_sfm_backend_cli.py
git commit -m "feat: resize SRT pose frames before COLMAP"
```

---

### Task 3: Build Fixed-Track COLMAP Commands with Matching Intrinsics

**Files:**
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/srt/fixed_track_visual_pose.py`
- Modify: `cadscene/cli/build_srt_fixed_track_visual_pose.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Modify: `tests/srt/test_fixed_track_visual_pose.py`
- Modify: `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`

**Interfaces:**
- Consumes: the Task 1 resolution helpers and `settings["reconstruction_resolution"]`.
- Produces: a fixed-track SfM command whose resize, `max_image_size`, PINHOLE parameters, and device agree; final trajectory/scene/report metadata separates render size from solve size.

- [ ] **Step 1: Add failing adapter tests for all three choices**

Update `_fixed_track_inputs` in `tests/projects/test_workflow_adapters.py` to accept the setting:

```python
def _fixed_track_inputs(
    tmp_path: Path,
    *,
    include_fov: bool = True,
    reconstruction_resolution: str | None = "1080p",
) -> AdapterInputs:
```

```python
    if reconstruction_resolution is not None:
        settings["reconstruction_resolution"] = reconstruction_resolution
```

Replace the fixed-track command assertions with:

```python
    assert adapter.version == "3"
    assert sfm[sfm.index("--reconstruction-height") + 1] == "1080"
    assert sfm[sfm.index("--max-image-size") + 1] == "1920"
    assert sfm[sfm.index("--device") + 1] == "auto"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "1321.32664365,1321.32664365,960,540"
    )
```

Add two focused cases:

```python
def test_fixed_track_720p_command_scales_intrinsics(tmp_path: Path) -> None:
    inputs = _fixed_track_inputs(
        tmp_path, reconstruction_resolution="720p"
    )
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )

    sfm = adapter.build_commands(adapter.prepare_inputs(inputs))[0]

    assert sfm[sfm.index("--reconstruction-height") + 1] == "720"
    assert sfm[sfm.index("--max-image-size") + 1] == "1280"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "880.884429102,880.884429102,640,360"
    )


def test_fixed_track_source_command_keeps_source_intrinsics(tmp_path: Path) -> None:
    inputs = _fixed_track_inputs(
        tmp_path, reconstruction_resolution="source"
    )
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )

    sfm = adapter.build_commands(adapter.prepare_inputs(inputs))[0]

    assert "--reconstruction-height" not in sfm
    assert sfm[sfm.index("--max-image-size") + 1] == "3840"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "2642.6532873,2642.6532873,1920,1080"
    )
```

- [ ] **Step 2: Run the adapter tests and verify the old command fails**

Run: `python -m pytest tests/projects/test_workflow_adapters.py::test_fixed_track_adapter_runs_colmap_before_pose_transfer tests/projects/test_workflow_adapters.py::test_fixed_track_720p_command_scales_intrinsics tests/projects/test_workflow_adapters.py::test_fixed_track_source_command_keeps_source_intrinsics -q`

Expected: failures show adapter version `2`, missing resolution/max-size/device flags, and source-sized 1080p intrinsics.

- [ ] **Step 3: Resolve dimensions once in the adapter**

Import:

```python
from cadscene.sfm.resolution import (
    normalize_reconstruction_resolution,
    reconstruction_dimensions,
    target_height_for_resolution,
)
```

Replace the fixed-track width/height calculation in `_sfm_command` with:

```python
            source_width = int(metadata.get("width", 0))
            source_height = int(metadata.get("height", 0))
            fov = float(settings.get("horizontal_fov_deg", 0.0))
            if source_width <= 0 or source_height <= 0 or not 1.0 < fov < 179.0:
                raise ValueError("fixed-track COLMAP requires valid video size and FOV")
            resolution = normalize_reconstruction_resolution(
                settings.get("reconstruction_resolution")
            )
            width, height = reconstruction_dimensions(
                source_width, source_height, resolution
            )
            focal = width / (2.0 * math.tan(math.radians(fov) * 0.5))
            camera_params = ",".join(
                f"{value:.12g}"
                for value in (focal, focal, width * 0.5, height * 0.5)
            )
            command.extend(
                [
                    "--backend", "colmap_cli",
                    "--device", str(inputs.parameters.get("device", "auto")),
                    "--max-image-size", str(max(width, height)),
                    "--camera-model", "PINHOLE",
                    "--camera-params", camera_params,
                    "--no-refine-focal-length",
                ]
            )
            target_height = target_height_for_resolution(resolution)
            if target_height is not None and source_height > target_height:
                command.extend(["--reconstruction-height", str(target_height)])
```

Prevent a duplicate `--device` when the generic parameter loop runs:

```python
        for key in (
            ("gpu_index",)
            if self.name == "srt_fixed_track_visual_pose"
            else ("backend", "device", "gpu_index")
        ):
```

Change the registered adapter version:

```python
                name="srt_fixed_track_visual_pose",
                version="3",
```

- [ ] **Step 4: Validate and serialize the enum in the build configuration**

Add to `FixedTrackVisualPoseConfig`:

```python
    reconstruction_resolution: str = "1080p"
```

Import and normalize it in `__post_init__`:

```python
from cadscene.sfm.resolution import normalize_reconstruction_resolution
```

```python
        normalize_reconstruction_resolution(self.reconstruction_resolution)
```

Deserialize and serialize it:

```python
            reconstruction_resolution=normalize_reconstruction_resolution(
                value.get("reconstruction_resolution")
            ),
```

```python
            "reconstruction_resolution": self.reconstruction_resolution,
```

Pass it from `_fixed_track_config_payload`:

```python
            "reconstruction_resolution": normalize_reconstruction_resolution(
                settings.get("reconstruction_resolution")
            ),
```

Add this assertion to `test_config_round_trip_preserves_uniform_xyz_offset` in `tests/srt/test_fixed_track_visual_pose.py`:

```python
    assert restored.reconstruction_resolution == "1080p"
```

- [ ] **Step 5: Carry actual solve dimensions into fixed-track artifacts**

Extend `_build_payloads`:

```python
    reconstruction_image_size: tuple[int, int],
```

Create one shared diagnostic fragment at the start of the function:

```python
    solve_metadata = {
        "reconstruction_resolution": config.reconstruction_resolution,
        "source_video_size": [width, height],
        "reconstruction_image_size": [
            int(reconstruction_image_size[0]),
            int(reconstruction_image_size[1]),
        ],
    }
```

Merge `**solve_metadata` into trajectory `meta`, scene `meta`, and `diagnostics`. Add report lines:

```python
        f"- 姿态解算档位：{config.reconstruction_resolution}\n"
        f"- 源视频尺寸：{width}×{height}\n"
        f"- 实际姿态解算尺寸：{reconstruction_image_size[0]}×{reconstruction_image_size[1]}\n"
```

After loading the COLMAP trajectory in `main`, pass its real dimensions:

```python
            reconstruction_image_size=(
                int(reconstruction.width), int(reconstruction.height)
            ),
```

In `tests/cli/test_build_srt_fixed_track_visual_pose_cli.py`, change the fixture reconstruction to `1920×1080` and add:

```python
    scene = json.loads(
        (output_root / "p1" / "clip-1" / "05_viewer_scene" / "sfm_viewer_scene.json")
        .read_text(encoding="utf-8")
    )
    assert scene["meta"]["source_video_size"] == [3840, 2160]
    assert scene["meta"]["reconstruction_image_size"] == [1920, 1080]
    assert scene["meta"]["reconstruction_resolution"] == "1080p"
```

- [ ] **Step 6: Run adapter, fixed-track, and builder tests**

Run: `python -m pytest tests/projects/test_workflow_adapters.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py -q`

Expected: all tests pass; fixed SRT positions and diagnostic point-cloud behavior remain unchanged.

- [ ] **Step 7: Commit the orchestration and diagnostics**

```bash
git diff -- cadscene/srt/fixed_track_visual_pose.py tests/srt/test_fixed_track_visual_pose.py
git add -p cadscene/srt/fixed_track_visual_pose.py tests/srt/test_fixed_track_visual_pose.py
git add cadscene/projects/workflow_adapters.py cadscene/cli/build_srt_fixed_track_visual_pose.py tests/projects/test_workflow_adapters.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py
git diff --cached --check
git commit -m "feat: configure fixed-track COLMAP solve resolution"
```

---

### Task 4: Persist the Setting and Expose It Conditionally in Project Setup

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Modify: `tests/projects/test_fixed_track_visual_pose_configuration.py`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: `normalize_reconstruction_resolution()` from Task 1.
- Produces: API and snapshots with `reconstruction_resolution`, legacy defaulting to `1080p`, and a selector hidden outside the missing-attitude workflow.

- [ ] **Step 1: Add failing service/API tests**

Update the expected stored settings in `tests/projects/test_fixed_track_visual_pose_configuration.py`:

```python
    assert selected.manual_definition["srt_fixed_track_visual_pose"] == {
        "schema_version": 2,
        "horizontal_fov_deg": 72,
        "reconstruction_resolution": "720p",
        "route_offset_xyz_m": [2.0, -1.0, 5.0],
    }
```

Pass `reconstruction_resolution="720p"` in that test, and add:

```python
def test_fixed_track_resolution_defaults_and_rejects_unknown_values(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)

    updated = service.update_srt_fixed_track_visual_pose_settings(
        "p1",
        "clip-1",
        expected_revision=repositories.clips.load("p1").revision,
        horizontal_fov_deg=72.0,
    )
    assert updated.clips[0].manual_definition[
        "srt_fixed_track_visual_pose"
    ]["reconstruction_resolution"] == "1080p"

    with pytest.raises(ValueError, match="reconstruction_resolution"):
        service.update_srt_fixed_track_visual_pose_settings(
            "p1",
            "clip-1",
            expected_revision=updated.revision,
            horizontal_fov_deg=72.0,
            reconstruction_resolution="4k",
        )


def test_legacy_fixed_track_settings_default_in_adapter_parameters(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=(replace(
                value.clips[0],
                manual_definition={
                    "srt_fixed_track_visual_pose": {
                        "schema_version": 1,
                        "horizontal_fov_deg": 72,
                        "route_offset_xyz_m": [0.0, 0.0, 0.0],
                    }
                },
            ),),
        ),
    )

    parameters = _fixed_track_visual_pose_adapter_parameters(
        service.projects_root,
        repositories.project.load("p1"),
        repositories.clips.load("p1").clips[0],
    )

    assert parameters["srt_fixed_track_visual_pose"][
        "reconstruction_resolution"
    ] == "1080p"
```

Add `"reconstruction_resolution": "720p"` to the PATCH request and assert it is returned in both the response and snapshot.

- [ ] **Step 2: Run service tests and verify missing parameter support**

Run: `python -m pytest tests/projects/test_fixed_track_visual_pose_configuration.py -q`

Expected: failures mention the unexpected service keyword and missing/default resolution field.

- [ ] **Step 3: Validate and persist the setting in service/API code**

Import in `cadscene/projects/service.py`:

```python
from cadscene.sfm.resolution import normalize_reconstruction_resolution
```

Extend the service signature:

```python
        reconstruction_resolution: str = "1080p",
```

Normalize it and build the versioned setting:

```python
        resolution = normalize_reconstruction_resolution(
            reconstruction_resolution
        )
        settings = {
            "schema_version": 2,
            "horizontal_fov_deg": fov,
            "reconstruction_resolution": resolution,
            "route_offset_xyz_m": offset,
        }
```

In `_fixed_track_visual_pose_adapter_parameters`, copy and normalize legacy settings:

```python
    normalized_settings = dict(settings)
    normalized_settings["reconstruction_resolution"] = (
        normalize_reconstruction_resolution(
            normalized_settings.get("reconstruction_resolution")
        )
    )
```

Return `normalized_settings` instead of `dict(settings)`.

Pass the API field with a backward-compatible default:

```python
            reconstruction_resolution=str(
                payload.get("reconstruction_resolution", "1080p")
            ),
```

- [ ] **Step 4: Run service/API tests**

Run: `python -m pytest tests/projects/test_fixed_track_visual_pose_configuration.py tests/projects/test_full_pose_configuration.py -q`

Expected: all tests pass, including full-pose configuration regressions.

- [ ] **Step 5: Add failing conditional frontend assertions**

Extend `test_srt_configuration_dialog_conditionally_supports_fixed_track_visual_pose`:

```python
    assert 'id="reconstructionResolutionField"' in html
    assert 'id="reconstructionResolutionInput"' in html
    assert '<option value="1080p">1080p（推荐）</option>' in html
    assert '<option value="720p">720p（快速）</option>' in html
    assert '<option value="source">原始分辨率</option>' in html
    assert '$("#reconstructionResolutionField").hidden = !fixedTrack;' in script
    assert 'settings.reconstruction_resolution || "1080p"' in script
    assert "reconstruction_resolution: reconstructionResolution" in script
```

Run: `python -m pytest tests/viewer/test_project_workspace_static.py::test_srt_configuration_dialog_conditionally_supports_fixed_track_visual_pose -q`

Expected: failure because the selector does not exist.

- [ ] **Step 6: Add the project setup selector and payload field**

Add inside `.full-pose-fields`:

```html
<label id="reconstructionResolutionField" hidden>
  <span>姿态解算分辨率</span>
  <select id="reconstructionResolutionInput">
    <option value="1080p">1080p（推荐）</option>
    <option value="720p">720p（快速）</option>
    <option value="source">原始分辨率</option>
  </select>
  <small>仅影响缺失姿态时的 COLMAP 抽帧与解算；预览和最终渲染仍使用原视频。</small>
</label>
```

In `openFullPoseDialog`:

```javascript
    $("#reconstructionResolutionField").hidden = !fixedTrack;
    $("#reconstructionResolutionInput").value = fixedTrack
      ? (settings.reconstruction_resolution || "1080p")
      : "1080p";
```

In `saveFullPoseSettings`, read and submit the value only for fixed track:

```javascript
    const reconstructionResolution = $("#reconstructionResolutionInput").value;
```

```javascript
                reconstruction_resolution: reconstructionResolution,
```

Bump the project workspace script URL:

```html
<script src="project_workspace.js?v=20260904-srt-resolution-v1"></script>
```

- [ ] **Step 7: Run frontend and service regression tests**

Run: `python -m pytest tests/viewer/test_project_workspace_static.py tests/projects/test_fixed_track_visual_pose_configuration.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit only this task's hunks**

Because `service.py` and `project_workspace.js` already contain user changes, inspect and stage only the resolution hunks:

```bash
git diff -- cadscene/projects/service.py apps/project_workspace/project_workspace.js
git add -p cadscene/projects/service.py apps/project_workspace/project_workspace.js
git add cadscene/projects/http_api.py tests/projects/test_fixed_track_visual_pose_configuration.py apps/project_workspace/index.html tests/viewer/test_project_workspace_static.py
git diff --cached --check
git commit -m "feat: configure missing SRT pose resolution"
```

---

### Task 5: Show Solve Parameters in the Workbench and Run End-to-End Regression

**Files:**
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/integration/test_srt_fixed_track_visual_pose_workflow.py`
- Test: `tests/integration/test_srt_full_pose_workflow.py`

**Interfaces:**
- Consumes: `sfmScene.meta.reconstruction_resolution`, `source_video_size`, and `reconstruction_image_size` produced by Task 3.
- Produces: a workbench information line that labels solve dimensions separately from source/render dimensions.

- [ ] **Step 1: Add the failing workbench diagnostic assertion**

Extend `test_fixed_track_workbench_shows_route_and_skips_sfm_and_quality`:

```python
    assert "姿态解算：${solveResolution}" in viewer
    assert "${solveSize[0]}×${solveSize[1]}" in viewer
    assert "源视频/渲染：${sourceSize[0]}×${sourceSize[1]}" in viewer
```

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py::test_fixed_track_workbench_shows_route_and_skips_sfm_and_quality -q`

Expected: failure because the solve diagnostic line is absent.

- [ ] **Step 2: Render the fixed-track solve metadata**

In `updateSfmInfoPanel`, before constructing `info.textContent` for fixed track:

```javascript
    const solveSize = Array.isArray(sfmScene.meta?.reconstruction_image_size)
      ? sfmScene.meta.reconstruction_image_size
      : null;
    const sourceSize = Array.isArray(sfmScene.meta?.source_video_size)
      ? sfmScene.meta.source_video_size
      : null;
    const solveResolution = sfmScene.meta?.reconstruction_resolution || "未记录";
    const fixedTrackSolveInfo = workflow === "srt_fixed_track_visual_pose"
      && solveSize && sourceSize
      ? `姿态解算：${solveResolution} / ${solveSize[0]}×${solveSize[1]}；源视频/渲染：${sourceSize[0]}×${sourceSize[1]}`
      : "";
```

Insert `fixedTrackSolveInfo` into the existing array before warning lines:

```javascript
      fixedTrackSolveInfo,
```

Bump the affected browser assets in `apps/web_camera_viewer/index.html`:

```html
<script src="./viewer_legacy.js?v=20260904-srt-resolution-v1"></script>
<script src="./workflow.js?v=20260904-srt-resolution-v1"></script>
```

- [ ] **Step 3: Run all focused tests**

Run: `python -m pytest tests/sfm/test_resolution.py tests/sfm/test_reconstruction.py tests/cli/test_run_sfm_cli.py tests/cli/test_run_sfm_backend_cli.py tests/projects/test_workflow_adapters.py tests/projects/test_fixed_track_visual_pose_configuration.py tests/srt/test_fixed_track_visual_pose.py tests/cli/test_build_srt_fixed_track_visual_pose_cli.py tests/viewer/test_project_workspace_static.py tests/viewer/test_workflow_ui_static.py -q`

Expected: all focused tests pass.

- [ ] **Step 4: Run workflow integration regressions**

Run: `python -m pytest tests/integration/test_srt_fixed_track_visual_pose_workflow.py tests/integration/test_srt_full_pose_workflow.py -q`

Expected: both fixed-track and full-pose integration suites pass; full pose still bypasses COLMAP.

- [ ] **Step 5: Inspect repository hygiene and staged scope**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: only the task's intended files plus the pre-existing user-owned modifications are listed; none of the unrelated user-owned files are staged.

- [ ] **Step 6: Run the complete automated suite**

Run: `python -m pytest -q`

Expected: the repository's complete test suite passes with no regression in SfM-only, pure-rotation, complete-SRT, fixed-track, render, or project-recovery workflows.

- [ ] **Step 7: Commit the workbench diagnostic**

```bash
git add apps/web_camera_viewer/viewer_legacy.js apps/web_camera_viewer/index.html tests/viewer/test_workflow_ui_static.py
git diff --cached --check
git commit -m "feat: show SRT pose solve dimensions in workbench"
```

- [ ] **Step 8: Perform a local smoke test with the actual 4K project**

After resolving and stopping only an existing listener whose command line is this repository's `cadscene.cli.serve_viewer`, start the service from PowerShell with:

```powershell
$resolutionWorktree = 'D:\zjic2026\cadscene_workbench\.worktrees\srt-full-pose-cad-georeference'
$resolutionStorage = 'D:\zjic2026\cadscene_workbench\work\srt-full-pose-ui-test'
Start-Process -FilePath python -ArgumentList @(
  '-m', 'cadscene.cli.serve_viewer',
  '--bind', '127.0.0.1',
  '--port', '8310',
  '--root', $resolutionWorktree,
  '--storage-root', $resolutionStorage
) -WorkingDirectory $resolutionWorktree -WindowStyle Hidden `
  -RedirectStandardOutput "$resolutionStorage\viewer-8310.stdout.log" `
  -RedirectStandardError "$resolutionStorage\viewer-8310.stderr.log"
```

Open `http://127.0.0.1:8310/apps/project_library/`. In a missing-attitude SRT project, verify these observable outcomes:

1. The dialog defaults to `1080p（推荐）`; full-pose SRT hides the selector.
2. Saving `720p` creates a new attempt rather than reusing a prior resolution result.
3. Progress explicitly reports `3840×2160 → 1280×720` and the effective CPU/CUDA device.
4. Generated PNGs under `02_sfm/images` are `1280×720`.
5. `02_sfm/sfm_stats.json` records source size, solve size, FOV-derived PINHOLE parameters, backend, and device.
6. The workbench displays the SRT route, COLMAP attitudes/frustums, optional sparse cloud, `720p / 1280×720` solve size, and `3840×2160` source/render size.
7. Keyframe refinement and route fitting still allow XYZ/yaw/pitch/roll; rendering remains a separate action and uses the original 4K video.

Expected: all seven observations hold. If the device is CPU, diagnostics must state CPU fallback; the pipeline must not invoke the OpenCV attitude route.
