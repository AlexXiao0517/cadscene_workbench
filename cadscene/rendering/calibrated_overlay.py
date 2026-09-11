from __future__ import annotations

import csv
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Callable, Mapping, Sequence

import cv2
import numpy as np
from scipy.spatial import cKDTree

from cadscene.cad.loader import load_cad_bundle
from cadscene.cad.projection import world_from_camera_rotation
from cadscene.cad.text_annotations import load_design_text_annotations
from cadscene.core.camera import CameraState
from cadscene.rendering.calibration import CalibratedCameraModel, output_dimensions
from cadscene.rendering.overlay import RenderOverlayResult


@dataclass(frozen=True)
class CalibratedRenderConfig:
    video_path: str | Path
    cad_dir: str | Path
    camera_path: str | Path
    camera_calibration: str | Path
    terrain_context: str | Path
    terrain_controls: str | Path
    output_video: str | Path
    cad_scale: float = 1.0
    origin_xy: tuple[float, float] = (0.0, 0.0)
    output_resolution: str = "1080p"
    overlay_linewidth: int = 2
    overlay_alpha: float = 0.92
    max_distance_m: float = 350.0
    fade_start_m: float = 260.0
    output_fps: float = 30.0
    ffmpeg_executable: str = "ffmpeg"


def project_world_points(
    points_world: np.ndarray,
    *, center: np.ndarray,
    world_from_camera: np.ndarray,
    camera: CalibratedCameraModel,
) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(points_world, dtype=np.float64).reshape(-1, 3)
    center = np.asarray(center, dtype=np.float64).reshape(3)
    rotation = np.asarray(world_from_camera, dtype=np.float64).reshape(3, 3)
    camera_points = (points - center) @ rotation
    depth = camera_points[:, 2]
    safe_depth = np.where(np.abs(depth) > 1e-12, depth, 1e-12)
    normalized = camera_points[:, :2] / safe_depth[:, None]
    radius2 = np.sum(normalized * normalized, axis=1)
    radial = 1.0 + camera.k1 * radius2 + camera.k2 * radius2 * radius2
    uv = np.column_stack(
        (
            camera.cx_px + camera.focal_px * normalized[:, 0] * radial,
            camera.cy_px + camera.focal_px * normalized[:, 1] * radial,
        )
    )
    return uv, depth


def render_calibrated_frame(
    frame: np.ndarray,
    state: CameraState,
    starts_xyz: np.ndarray,
    ends_xyz: np.ndarray,
    colors_bgr: np.ndarray,
    linewidths: np.ndarray,
    camera: CalibratedCameraModel,
    *, overlay_alpha: float = 0.92,
    max_distance_m: float = 350.0,
    fade_start_m: float = 260.0,
) -> np.ndarray:
    if frame.shape[1::-1] != (camera.width, camera.height):
        raise ValueError("frame size must match calibrated camera size")
    starts = np.asarray(starts_xyz, dtype=np.float64).reshape(-1, 3)
    ends = np.asarray(ends_xyz, dtype=np.float64).reshape(-1, 3)
    colors = np.asarray(colors_bgr, dtype=np.uint8).reshape(-1, 3)
    widths = np.asarray(linewidths, dtype=np.int32).reshape(-1)
    if not (len(starts) == len(ends) == len(colors) == len(widths)):
        raise ValueError("calibrated segment arrays must have matching lengths")
    center = np.asarray([state.camera_x, state.camera_y, state.camera_z], dtype=np.float64)
    spatial = (
        (np.maximum(starts[:, 0], ends[:, 0]) >= center[0] - max_distance_m)
        & (np.minimum(starts[:, 0], ends[:, 0]) <= center[0] + max_distance_m)
        & (np.maximum(starts[:, 1], ends[:, 1]) >= center[1] - max_distance_m)
        & (np.minimum(starts[:, 1], ends[:, 1]) <= center[1] + max_distance_m)
    )
    starts, ends, colors, widths = starts[spatial], ends[spatial], colors[spatial], widths[spatial]
    if not len(starts):
        return frame.copy()
    rotation = world_from_camera_rotation(state)
    uv0, depth0 = project_world_points(starts, center=center, world_from_camera=rotation, camera=camera)
    uv1, depth1 = project_world_points(ends, center=center, world_from_camera=rotation, camera=camera)
    valid = (depth0 > 0.05) & (depth1 > 0.05) & (depth0 <= max_distance_m) & (depth1 <= max_distance_m)
    valid &= np.isfinite(uv0).all(axis=1) & np.isfinite(uv1).all(axis=1)
    valid &= np.linalg.norm(uv1 - uv0, axis=1) <= max(camera.width, camera.height) * 0.5
    indices = np.flatnonzero(valid)
    if not len(indices):
        return frame.copy()
    if max_distance_m > fade_start_m:
        weights = np.clip(
            (max_distance_m - 0.5 * (depth0[indices] + depth1[indices]))
            / (max_distance_m - fade_start_m), 0.0, 1.0
        )
    else:
        weights = np.ones(len(indices))
    layer = frame.copy()
    mask = np.zeros(frame.shape[:2], dtype=np.float32)
    for selected, weight in zip(indices, weights):
        if weight <= 0:
            continue
        first = tuple(np.rint(uv0[selected]).astype(int))
        second = tuple(np.rint(uv1[selected]).astype(int))
        color = tuple(int(value) for value in colors[selected])
        width = max(1, int(widths[selected]))
        cv2.line(layer, first, second, color, width, cv2.LINE_AA)
        cv2.line(mask, first, second, float(weight), width, cv2.LINE_AA)
    alpha = np.clip(mask * float(overlay_alpha), 0.0, 1.0)[..., None]
    return np.rint(frame.astype(np.float32) * (1 - alpha) + layer.astype(np.float32) * alpha).astype(np.uint8)


def _load_exact_cameras(path: Path) -> dict[int, CameraState]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    cameras = {
        int(float(row["frame_index"])): CameraState.from_row(row)
        for row in rows
        if str(row.get("status", "ok")).lower() == "ok"
    }
    if not cameras:
        raise ValueError("camera path contains no usable exact frame")
    return cameras


def effective_camera_from_track(
    source_camera: CalibratedCameraModel,
    cameras: Mapping[int, CameraState],
) -> tuple[CalibratedCameraModel, float]:
    fovs = np.asarray([float(state.fov_deg) for state in cameras.values()])
    if not len(fovs) or not np.isfinite(fovs).all():
        raise ValueError("camera track requires a finite global horizontal FOV")
    if float(np.max(fovs) - np.min(fovs)) > 0.1:
        raise ValueError("camera track must use one global horizontal FOV")
    effective_fov = float(np.mean(fovs))
    return source_camera.with_horizontal_fov(effective_fov), effective_fov


def _hex_bgr(value: object) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 6:
        try:
            red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
            return blue, green, red
        except ValueError:
            pass
    return 255, 255, 255


def _flatten_design(
    path: Path,
    *,
    origin_xy: tuple[float, float],
    cad_scale: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if not path.is_file():
        return np.empty((0, 2)), np.empty((0, 2)), np.empty((0, 3), dtype=np.uint8)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    coordinate_mode = str((payload.get("meta") or {}).get("coordinate_mode", "cad_world"))
    starts, ends, colors = [], [], []
    origin = np.asarray(origin_xy, dtype=np.float64)
    for layer in payload.get("layers", ()):
        if not isinstance(layer, Mapping):
            continue
        layer_color = _hex_bgr(layer.get("color"))
        for entity in layer.get("entities", ()):
            if not isinstance(entity, Mapping):
                continue
            points = np.asarray(entity.get("world_points", ()), dtype=np.float64)
            if points.ndim != 2 or points.shape[1] < 2 or len(points) < 2:
                continue
            xy = points[:, :2]
            if coordinate_mode == "cad_world":
                xy = (xy - origin) * cad_scale
            color = _hex_bgr(entity.get("color")) if entity.get("color") else layer_color
            for first, second in zip(xy[:-1], xy[1:]):
                starts.append(first)
                ends.append(second)
                colors.append(color)
    return (
        np.asarray(starts, dtype=np.float64).reshape(-1, 2),
        np.asarray(ends, dtype=np.float64).reshape(-1, 2),
        np.asarray(colors, dtype=np.uint8).reshape(-1, 3),
    )


def _flatten_cad(
    cad,
    *,
    cad_dir: Path,
    origin_xy: tuple[float, float],
    cad_scale: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    design = _flatten_design(
        cad_dir / "design.json", origin_xy=origin_xy, cad_scale=cad_scale
    )
    if len(design[0]):
        return design
    starts, ends, colors = [], [], []
    for line in (*cad.refs, *cad.edges, *cad.centers):
        for first, second in zip(line.points[:-1], line.points[1:]):
            starts.append(first[:2])
            ends.append(second[:2])
            colors.append(line.color_bgr or (255, 255, 255))
    return (
        np.asarray(starts, dtype=np.float64).reshape(-1, 2),
        np.asarray(ends, dtype=np.float64).reshape(-1, 2),
        np.asarray(colors, dtype=np.uint8).reshape(-1, 3),
    )


_FONT_CACHE: dict[int, object] = {}


def _draw_text_annotations(
    frame: np.ndarray,
    *,
    state: CameraState,
    points_xyz: np.ndarray,
    labels: Sequence[str],
    heights_m: np.ndarray,
    camera: CalibratedCameraModel,
    max_distance_m: float,
) -> np.ndarray:
    if not len(points_xyz):
        return frame
    center = np.asarray([state.camera_x, state.camera_y, state.camera_z], dtype=np.float64)
    uv, depth = project_world_points(
        points_xyz,
        center=center,
        world_from_camera=world_from_camera_rotation(state),
        camera=camera,
    )
    valid = (
        (depth > 0.05)
        & (depth <= max_distance_m)
        & np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= -80)
        & (uv[:, 0] <= camera.width + 80)
        & (uv[:, 1] >= -40)
        & (uv[:, 1] <= camera.height + 40)
    )
    indices = np.flatnonzero(valid)
    if not len(indices):
        return frame
    indices = indices[np.argsort(depth[indices])[:240]]
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return frame
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    windows_fonts = (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    )
    font_path = next((path for path in windows_fonts if path.is_file()), None)
    for index in indices:
        size = int(np.clip(round(camera.focal_px * heights_m[index] / depth[index]), 11, 36))
        if size not in _FONT_CACHE:
            _FONT_CACHE[size] = (
                ImageFont.truetype(str(font_path), size)
                if font_path is not None
                else ImageFont.load_default()
            )
        draw.text(
            (float(uv[index, 0]), float(uv[index, 1])),
            str(labels[index]),
            font=_FONT_CACHE[size],
            fill=(255, 255, 255),
            stroke_width=1,
            stroke_fill=(0, 0, 0),
            anchor="mm",
        )
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)


def _drape(
    starts: np.ndarray,
    ends: np.ndarray,
    controls_path: Path,
    mode: str,
    *,
    fallback_z: float = 0.0,
):
    if mode == "relative":
        return np.column_stack((starts, np.full(len(starts), fallback_z))), np.column_stack((ends, np.full(len(ends), fallback_z))), np.ones(len(starts), dtype=bool)
    arrays = np.load(controls_path)
    samples = [arrays["points_xyz"]]
    segment_starts = arrays["segment_starts_xyz"]
    segment_ends = arrays["segment_ends_xyz"]
    for first, second in zip(segment_starts, segment_ends):
        count = max(1, int(np.ceil(np.linalg.norm(second[:2] - first[:2]) / 2.0)))
        samples.append(first[None, :] + (second - first)[None, :] * np.linspace(0, 1, count + 1)[:, None])
    points = np.vstack([item for item in samples if len(item)]) if any(len(item) for item in samples) else np.empty((0, 3))
    if not len(points):
        return np.column_stack((starts, np.full(len(starts), fallback_z))), np.column_stack((ends, np.full(len(ends), fallback_z))), np.ones(len(starts), dtype=bool)
    tree = cKDTree(points[:, :2])
    d0, i0 = tree.query(starts)
    d1, i1 = tree.query(ends)
    z0 = np.where(d0 <= 160.0, points[i0, 2], fallback_z)
    z1 = np.where(d1 <= 160.0, points[i1, 2], fallback_z)
    keep = np.ones(len(starts), dtype=bool)
    return np.column_stack((starts, z0)), np.column_stack((ends, z1)), keep


def _ffmpeg_command(executable: str, width: int, height: int, fps: float, output: Path) -> list[str]:
    return [
        executable, "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s:v", f"{width}x{height}",
        "-r", f"{fps:g}", "-i", "pipe:0", "-an", "-c:v", "libx264",
        "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output),
    ]


def render_calibrated_overlay_video(
    config: CalibratedRenderConfig,
    *, progress_callback: Callable[[str, str, float], None] | None = None,
) -> RenderOverlayResult:
    started = perf_counter()
    source_camera = CalibratedCameraModel.from_json(config.camera_calibration)
    width, height = output_dimensions(source_camera.width, source_camera.height, config.output_resolution)
    cameras = _load_exact_cameras(Path(config.camera_path))
    effective_source_camera, effective_fov = effective_camera_from_track(
        source_camera, cameras
    )
    camera = effective_source_camera.scaled_to(width, height)
    context = json.loads(Path(config.terrain_context).read_text(encoding="utf-8-sig"))
    mode = str(context.get("terrain_mode", "relative"))
    fallback_z = float(
        context.get("cad_fallback_ground_m")
        or context.get("terrain_reference_ground_m")
        or 0.0
    )
    warnings = [str(item) for item in context.get("terrain_warnings", ())]
    cad_dir = Path(config.cad_dir)
    cad = load_cad_bundle(cad_dir, origin_xy=config.origin_xy, cad_scale=config.cad_scale)
    starts, ends, colors = _flatten_cad(
        cad,
        cad_dir=cad_dir,
        origin_xy=config.origin_xy,
        cad_scale=config.cad_scale,
    )
    starts3, ends3, keep = _drape(
        starts,
        ends,
        Path(config.terrain_controls),
        mode,
        fallback_z=fallback_z,
    )
    starts3, ends3, colors = starts3[keep], ends3[keep], colors[keep]
    widths = np.full(len(starts3), max(1, int(config.overlay_linewidth)), dtype=np.int32)
    design_path = cad_dir / "design.json"
    if design_path.is_file():
        text_annotations = load_design_text_annotations(
            design_path,
            origin_xy=config.origin_xy,
            cad_scale=config.cad_scale,
        )
        text_points3, _, text_keep = _drape(
            text_annotations.points_xy,
            text_annotations.points_xy,
            Path(config.terrain_controls),
            mode,
            fallback_z=fallback_z,
        )
        text_points3 = text_points3[text_keep]
        text_labels = tuple(
            label for label, selected in zip(text_annotations.labels, text_keep) if selected
        )
        text_heights = text_annotations.heights_m[text_keep]
    else:
        text_points3 = np.empty((0, 3), dtype=np.float64)
        text_labels = ()
        text_heights = np.empty(0, dtype=np.float64)
    source = cv2.VideoCapture(str(config.video_path))
    if not source.isOpened():
        raise RuntimeError(f"video cannot be opened: {config.video_path}")
    frame_count = int(source.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    output = Path(config.output_video)
    output.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(_ffmpeg_command(config.ffmpeg_executable, width, height, config.output_fps, output), stdin=subprocess.PIPE)
    rendered = 0
    try:
        while True:
            ok, frame = source.read()
            if not ok:
                break
            if rendered not in cameras:
                raise ValueError(f"exact camera pose is missing for frame {rendered}")
            if frame.shape[1::-1] != (width, height):
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
            overlaid = render_calibrated_frame(
                frame, cameras[rendered], starts3, ends3, colors, widths, camera,
                overlay_alpha=config.overlay_alpha,
                max_distance_m=config.max_distance_m,
                fade_start_m=config.fade_start_m,
            )
            overlaid = _draw_text_annotations(
                overlaid,
                state=cameras[rendered],
                points_xyz=text_points3,
                labels=text_labels,
                heights_m=text_heights,
                camera=camera,
                max_distance_m=config.max_distance_m,
            )
            if process.stdin is None:
                raise RuntimeError("FFmpeg input pipe is unavailable")
            process.stdin.write(overlaid.tobytes())
            rendered += 1
            if progress_callback and (rendered == 1 or rendered % 30 == 0):
                progress_callback("rendering_frames", f"rendered frame {rendered}/{frame_count}", rendered / max(1, frame_count))
            if rendered == 1 or rendered % 30 == 0 or rendered == frame_count:
                print(
                    f"[render] frame {rendered}/{frame_count} source_frame={rendered - 1}",
                    flush=True,
                )
        if process.stdin is not None:
            process.stdin.close()
        returncode = process.wait()
        if returncode != 0:
            raise RuntimeError(f"FFmpeg H.264 encoder failed with code {returncode}")
    finally:
        source.release()
        if process.poll() is None:
            process.kill()
    stats = {
        "frame_count": frame_count, "rendered_frame_count": rendered,
        "width": width, "height": height, "fps": config.output_fps,
        "video_codec": "h264", "camera_interpolation": "exact_frame",
        "camera_path_frame_count": len(cameras),
        "camera_calibration": camera.to_dict(), "terrain_mode": mode,
        "effective_horizontal_fov_deg": effective_fov,
        "terrain_coverage": float(context.get("terrain_coverage") or 0.0),
        "terrain_source_fingerprints": list(
            context.get("terrain_source_fingerprints", ())
        ),
        "cad_polyline_count": len(starts3), "elapsed_sec": perf_counter() - started,
        "cad_text_count": len(text_points3),
        "output_resolution": config.output_resolution,
    }
    return RenderOverlayResult(output_video=output, stats=stats, warnings=warnings)
