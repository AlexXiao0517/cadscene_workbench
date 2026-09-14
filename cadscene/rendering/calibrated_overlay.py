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
from cadscene.cad.text_annotations import load_design_text_annotations, load_dxf_text_annotations
from cadscene.core.camera import CameraState
from cadscene.rendering.calibration import CalibratedCameraModel, output_dimensions
from cadscene.rendering.cad_region import CadRenderRegion, SegmentVisibilityIndex, region_from_track
from cadscene.rendering.overlay import RenderOverlayResult, _cad_lines, _densify_points_for_projection, style_for_kind


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
    max_distance_m: float | None = None
    fade_start_m: float | None = None
    output_fps: float = 30.0
    ffmpeg_executable: str = "ffmpeg"
    cad_region_bounds: tuple[float, float, float, float] | None = None
    cad_region_margin_m: float = 100.0
    cad_region_lookahead_m: float = 1000.0

    def __post_init__(self):
        if self.cad_region_bounds is not None:
            CadRenderRegion(self.cad_region_bounds)
        if (not np.isfinite([self.cad_region_margin_m, self.cad_region_lookahead_m]).all()
                or self.cad_region_margin_m < 0 or self.cad_region_lookahead_m <= 0):
            raise ValueError("CAD region margin/lookahead must be finite and nonnegative/positive")


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
    max_distance_m: float | None = None,
    fade_start_m: float | None = None,
    visibility_index: SegmentVisibilityIndex | None = None,
) -> np.ndarray:
    if frame.shape[1::-1] != (camera.width, camera.height):
        raise ValueError("frame size must match calibrated camera size")
    starts = np.asarray(starts_xyz, dtype=np.float64).reshape(-1, 3)
    ends = np.asarray(ends_xyz, dtype=np.float64).reshape(-1, 3)
    colors = np.asarray(colors_bgr, dtype=np.uint8).reshape(-1, 3)
    widths = np.asarray(linewidths, dtype=np.int32).reshape(-1)
    if not (len(starts) == len(ends) == len(colors) == len(widths)):
        raise ValueError("calibrated segment arrays must have matching lengths")
    padding = float(np.max(widths, initial=0)) / 2 + 3
    if visibility_index is not None:
        if visibility_index.count != len(starts):
            raise ValueError("visibility index must match the segment arrays")
        selected = visibility_index.query(state, camera, padding_px=padding)
        starts, ends, colors, widths = starts[selected], ends[selected], colors[selected], widths[selected]
    center = np.asarray([state.camera_x, state.camera_y, state.camera_z], dtype=np.float64)
    distance = float('inf') if max_distance_m is None else max_distance_m
    spatial = (
        (np.maximum(starts[:, 0], ends[:, 0]) >= center[0] - distance)
        & (np.minimum(starts[:, 0], ends[:, 0]) <= center[0] + distance)
        & (np.maximum(starts[:, 1], ends[:, 1]) >= center[1] - distance)
        & (np.minimum(starts[:, 1], ends[:, 1]) <= center[1] + distance)
    )
    starts, ends, colors, widths = starts[spatial], ends[spatial], colors[spatial], widths[spatial]
    if not len(starts):
        return frame.copy()
    rotation = world_from_camera_rotation(state)
    uv0, depth0 = project_world_points(starts, center=center, world_from_camera=rotation, camera=camera)
    uv1, depth1 = project_world_points(ends, center=center, world_from_camera=rotation, camera=camera)
    valid = (depth0 > 0.05) & (depth1 > 0.05) & (depth0 <= distance) & (depth1 <= distance)
    valid &= np.isfinite(uv0).all(axis=1) & np.isfinite(uv1).all(axis=1)
    limit = float(max(camera.width, camera.height) * 8)
    valid &= (np.abs(uv0) <= limit).all(axis=1) & (np.abs(uv1) <= limit).all(axis=1)
    valid &= (
        (np.maximum(uv0[:, 0], uv1[:, 0]) >= -padding)
        & (np.minimum(uv0[:, 0], uv1[:, 0]) <= camera.width + padding)
        & (np.maximum(uv0[:, 1], uv1[:, 1]) >= -padding)
        & (np.minimum(uv0[:, 1], uv1[:, 1]) <= camera.height + padding)
    )
    if max_distance_m is not None and fade_start_m is not None and max_distance_m > fade_start_m:
        fade0 = np.clip((max_distance_m - depth0) / (max_distance_m - fade_start_m), 0., 1.)
        fade1 = np.clip((max_distance_m - depth1) / (max_distance_m - fade_start_m), 0., 1.)
        weights = 0.5 * (fade0 + fade1)
    else:
        weights = np.ones(len(depth0))
    indices = np.flatnonzero(valid & (weights > 0))
    if not len(indices):
        return frame.copy()
    segments = np.rint(np.stack((uv0[indices], uv1[indices]), axis=1)).astype(np.int32).reshape(-1, 2, 1, 2)
    selected_widths = widths[indices]
    layer = frame.copy()
    styles, style_ids = np.unique(np.column_stack((colors[indices], selected_widths)), axis=0, return_inverse=True)
    for style_id, style in enumerate(styles):
        cv2.polylines(layer, segments[style_ids == style_id], False,
                      tuple(int(v) for v in style[:3]), int(style[3]), lineType=cv2.LINE_AA)
    # Preserve the accepted backend's anti-aliased 12-band distance mask.
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    bands = np.clip((weights[indices] * 12 - 1e-9).astype(np.int32), 0, 11)
    for band in range(12):
        band_indices = np.flatnonzero(bands == band)
        band_widths = selected_widths[band_indices]
        for line_width in np.unique(band_widths):
            cv2.polylines(mask, segments[band_indices[band_widths == line_width]], False,
                          int(round((band + 1) / 12 * 255.)), int(line_width), lineType=cv2.LINE_AA)
    active = mask > 0
    alpha = mask[active].astype(np.float32)[:, None] / 255.
    alpha *= float(np.clip(overlay_alpha, 0., 1.))
    result = frame.copy()
    result[active] = (frame[active].astype(np.float32) * (1 - alpha)
                      + layer[active].astype(np.float32) * alpha).astype(np.uint8)
    return result


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
    route_xy: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    starts, ends, colors = [], [], []
    for line in _cad_lines(cad):
        if not len(line.points):
            continue
        points = _densify_points_for_projection(line.points, 20.0)
        for first, second in zip(points[:-1], points[1:]):
            starts.append(first[:2])
            ends.append(second[:2])
            colors.append(line.color_bgr or style_for_kind(line.kind).color_bgr)
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
    max_distance_m: float | None,
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
    distance = float('inf') if max_distance_m is None else max_distance_m
    valid = (
        (depth > 0.05)
        & (depth <= distance)
        & (np.linalg.norm(points_xyz[:, :2] - center[:2], axis=1) <= distance)
        & np.isfinite(uv).all(axis=1)
        & (uv[:, 0] >= 8)
        & (uv[:, 0] < camera.width - 8)
        & (uv[:, 1] >= 8)
        & (uv[:, 1] < camera.height - 8)
    )
    indices = np.flatnonzero(valid)
    if not len(indices):
        return frame
    indices = sorted(indices, key=lambda index: depth[index])
    accepted, boxes = [], []
    for index in indices:
        label = str(labels[index]).strip()
        if not label or "\ufffd" in label:
            continue
        label = label[:80]
        size = int(np.clip(camera.focal_px * heights_m[index] / depth[index] * 0.28, 12, 30))
        x, y = (int(round(value)) for value in uv[index])
        x += 5
        estimated_width = min(camera.width, max(size, int(len(label) * size * 0.78)))
        box = (x - 3, y - size, x + estimated_width + 3, y + size)
        if any(min(box[2], other[2]) > max(box[0], other[0])
               and min(box[3], other[3]) > max(box[1], other[1]) for other in boxes):
            continue
        accepted.append((x, y, label, size))
        boxes.append(box)
        if len(accepted) >= 24:
            break
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return frame
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image)
    windows_fonts = (
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
    )
    font_path = next((path for path in windows_fonts if path.is_file()), None)
    for x, y, label, size in accepted:
        if size not in _FONT_CACHE:
            _FONT_CACHE[size] = (
                ImageFont.truetype(str(font_path), size)
                if font_path is not None
                else ImageFont.load_default()
            )
        draw.text(
            (x, y),
            label,
            font=_FONT_CACHE[size],
            fill=(255, 255, 255),
            stroke_width=2,
            stroke_fill=(15, 15, 15),
            anchor="lm",
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
    samples = []
    segment_starts = arrays["segment_starts_xyz"]
    segment_ends = arrays["segment_ends_xyz"]
    for first, second in zip(segment_starts, segment_ends):
        count = max(1, int(np.ceil(np.linalg.norm(second[:2] - first[:2]) / 2.0)))
        samples.append(first[None, :] + (second - first)[None, :] * np.linspace(0, 1, count + 1)[:, None])
    if not samples:
        samples.append(arrays["points_xyz"])
    points = np.vstack([item for item in samples if len(item)]) if any(len(item) for item in samples) else np.empty((0, 3))
    if not len(points):
        return np.column_stack((starts, np.full(len(starts), fallback_z))), np.column_stack((ends, np.full(len(ends), fallback_z))), np.ones(len(starts), dtype=bool)
    tree = cKDTree(points[:, :2])
    d0, i0 = tree.query(starts)
    d1, i1 = tree.query(ends)
    z0 = np.where(d0 <= 160.0, points[i0, 2], fallback_z)
    z1 = np.where(d1 <= 160.0, points[i1, 2], fallback_z)
    keep = (d0 <= 160.0) & (d1 <= 160.0)
    return np.column_stack((starts, z0)), np.column_stack((ends, z1)), keep


def _drape_region(starts, ends, colors, controls_path, mode, *, fallback_z, region):
    # Preserve the original terrain slope before introducing boundary vertices.
    starts3, ends3, keep = _drape(starts, ends, controls_path, mode, fallback_z=fallback_z)
    starts3, ends3, colors = starts3[keep], ends3[keep], colors[keep]
    starts3, ends3, keep = region.clip_segments(starts3, ends3)
    return starts3, ends3, colors[keep]


def _prepare_text_annotations(
    cad_dir: Path, *, origin_xy: tuple[float, float], cad_scale: float,
    route_xy: np.ndarray, controls_path: Path, mode: str, fallback_z: float,
) -> tuple[np.ndarray, tuple[str, ...], np.ndarray]:
    raw_files = sorted((cad_dir / "raw_cad").glob("*.dxf"))
    if raw_files:
        annotations = load_dxf_text_annotations(raw_files[0], origin_xy=origin_xy, cad_scale=cad_scale)
    elif (cad_dir / "design.json").is_file():
        annotations = load_design_text_annotations(cad_dir / "design.json", origin_xy=origin_xy, cad_scale=cad_scale)
    else:
        return np.empty((0, 3)), (), np.empty(0)
    selected = np.arange(len(annotations.points_xy))
    points = annotations.points_xy[selected]
    points3, _, keep = _drape(points, points, controls_path, mode, fallback_z=fallback_z)
    points3 = points3[keep]
    points3[:, 2] += 0.3
    selected = selected[keep]
    return points3, tuple(annotations.labels[i] for i in selected), annotations.heights_m[selected]


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
    region = region_from_track(cameras.values(), camera, ground_z=fallback_z,
                               bounds=config.cad_region_bounds, margin_m=config.cad_region_margin_m,
                               lookahead_m=config.cad_region_lookahead_m)
    cad_dir = Path(config.cad_dir)
    cad = load_cad_bundle(cad_dir, origin_xy=config.origin_xy, cad_scale=config.cad_scale)
    route_xy = np.asarray([[state.camera_x, state.camera_y] for state in cameras.values()])
    starts, ends, colors = _flatten_cad(
        cad,
        cad_dir=cad_dir,
        origin_xy=config.origin_xy,
        cad_scale=config.cad_scale,
        route_xy=route_xy,
    )
    source_segment_count = len(starts)
    starts3, ends3, colors = _drape_region(
        starts,
        ends,
        colors,
        Path(config.terrain_controls),
        mode,
        fallback_z=fallback_z,
        region=region,
    )
    widths = np.full(len(starts3), max(1, int(config.overlay_linewidth)), dtype=np.int32)
    visibility_index = SegmentVisibilityIndex(starts3, ends3)
    text_points3, text_labels, text_heights = _prepare_text_annotations(
        cad_dir, origin_xy=config.origin_xy, cad_scale=config.cad_scale,
        route_xy=route_xy, controls_path=Path(config.terrain_controls),
        mode=mode, fallback_z=fallback_z,
    )
    text_keep = region.contains_points(text_points3)
    text_points3, text_heights = text_points3[text_keep], text_heights[text_keep]
    text_labels = tuple(label for label, keep in zip(text_labels, text_keep) if keep)
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
                visibility_index=visibility_index,
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
        "renderer_profile": "accepted_backend_at_v3_fixed_region",
        "source_segment_count": source_segment_count,
        "cad_region_bounds": region.bounds,
        "cad_region_method": "explicit" if config.cad_region_bounds is not None else "whole_track_envelope",
        "cad_region_margin_m": config.cad_region_margin_m,
        "cad_region_lookahead_m": config.cad_region_lookahead_m,
        "overlay_linewidth": config.overlay_linewidth,
        "overlay_alpha": config.overlay_alpha,
        "max_distance_m": config.max_distance_m,
        "fade_start_m": config.fade_start_m,
        "max_cad_labels": 24,
    }
    return RenderOverlayResult(output_video=output, stats=stats, warnings=warnings)
