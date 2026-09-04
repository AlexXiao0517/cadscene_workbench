"""Render an isolated Bentley XML aerial-triangulation calibration baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Mapping, Sequence

import cv2
import numpy as np

from cadscene.cad.loader import CadBundle, load_cad_bundle
from cadscene.cad.projection import world_from_camera_rotation
from cadscene.core.camera import CameraState
from cadscene.diagnostics.xml_at_reference import (
    build_adjusted_at_track,
    project_distorted_segments,
    srt_vertical_reference_m,
)
from cadscene.rendering.overlay import _cad_lines, _densify_points_for_projection
from cadscene.rendering.styles import style_for_kind
from cadscene.srt.bentley_pose_merge import (
    BentleyCameraModel,
    load_bentley_camera_model,
    load_bentley_pose_samples,
)
from cadscene.srt.georeference import CadGeoreference
from cadscene.srt.parser import load_srt_records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render CAD with adjusted Bentley XML poses and exact calibration."
    )
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--cad-dir", type=Path, required=True)
    parser.add_argument("--central-meridian", type=float, required=True)
    parser.add_argument("--cad-origin", type=float, nargs=2, required=True)
    parser.add_argument("--cad-scale", type=float, default=1.0)
    parser.add_argument("--start-frame", type=int, default=300)
    parser.add_argument("--end-frame", type=int, default=11439)
    parser.add_argument("--sample-every", type=int, default=5)
    parser.add_argument("--output-size", type=int, nargs=2, default=(1920, 1080))
    parser.add_argument("--max-distance-m", type=float, default=250.0)
    parser.add_argument("--fade-start-m", type=float, default=120.0)
    parser.add_argument("--overlay-alpha", type=float, default=0.88)
    parser.add_argument("--linewidth", type=int, default=2)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def validate_render_interval(
    *, start_frame: int, end_frame: int, sample_every: int
) -> None:
    if int(start_frame) < 0:
        raise ValueError("start-frame must be non-negative")
    if int(end_frame) < int(start_frame):
        raise ValueError("end-frame must be greater than or equal to start-frame")
    if int(sample_every) <= 0:
        raise ValueError("sample-every must be positive")


def resolve_ffmpeg(executable: str) -> str:
    resolved = shutil.which(str(executable))
    if resolved is None:
        raise FileNotFoundError(f"FFmpeg executable is unavailable: {executable}")
    return resolved


def flatten_cad_segments(
    cad: CadBundle, *, step_m: float = 20.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    starts: list[np.ndarray] = []
    ends: list[np.ndarray] = []
    colors: list[np.ndarray] = []
    for line in _cad_lines(cad):
        points = _densify_points_for_projection(line.points, step_m)
        if len(points) < 2:
            continue
        segment_count = len(points) - 1
        starts.append(points[:-1])
        ends.append(points[1:])
        color = line.color_bgr or style_for_kind(line.kind).color_bgr
        colors.append(np.repeat(np.asarray(color, dtype=np.uint8)[None, :], segment_count, axis=0))
    if not starts:
        raise ValueError("no CAD segments are available for XML AT rendering")
    return np.vstack(starts), np.vstack(ends), np.vstack(colors)


def render_calibrated_frame(
    frame: np.ndarray,
    camera: CameraState,
    starts_xy: np.ndarray,
    ends_xy: np.ndarray,
    camera_model: BentleyCameraModel,
    *,
    colors_bgr: np.ndarray | None = None,
    max_distance_m: float = 250.0,
    fade_start_m: float = 120.0,
    overlay_alpha: float = 0.88,
    linewidth: int = 2,
) -> np.ndarray:
    """Draw static CAD segments with adjusted XML extrinsics and calibration."""

    height, width = frame.shape[:2]
    if (width, height) != camera_model.image_size:
        raise ValueError("frame size must equal the XML calibrated image size")
    starts_value = np.asarray(starts_xy, dtype=np.float64)
    ends_value = np.asarray(ends_xy, dtype=np.float64)
    if starts_value.shape != ends_value.shape or starts_value.ndim != 2 or starts_value.shape[1] != 2:
        raise ValueError("CAD segment arrays must have matching shape (N, 2)")
    if colors_bgr is None:
        colors_value = np.repeat(
            np.asarray([[0, 255, 0]], dtype=np.uint8), len(starts_value), axis=0
        )
    else:
        colors_value = np.asarray(colors_bgr, dtype=np.uint8)
        if colors_value.shape != (len(starts_value), 3):
            raise ValueError("colors_bgr must have shape (N, 3)")

    center = np.asarray(
        [camera.camera_x, camera.camera_y, camera.camera_z], dtype=np.float64
    )
    radius = float(max_distance_m)
    if radius <= 0.0:
        raise ValueError("max_distance_m must be positive")
    spatial = (
        (np.maximum(starts_value[:, 0], ends_value[:, 0]) >= center[0] - radius)
        & (np.minimum(starts_value[:, 0], ends_value[:, 0]) <= center[0] + radius)
        & (np.maximum(starts_value[:, 1], ends_value[:, 1]) >= center[1] - radius)
        & (np.minimum(starts_value[:, 1], ends_value[:, 1]) <= center[1] + radius)
    )
    starts_value = starts_value[spatial]
    ends_value = ends_value[spatial]
    colors_value = colors_value[spatial]
    if not len(starts_value):
        return frame.copy()

    starts_world = np.column_stack(
        (starts_value, np.zeros(len(starts_value), dtype=np.float64))
    )
    ends_world = np.column_stack(
        (ends_value, np.zeros(len(ends_value), dtype=np.float64))
    )
    world_from_camera = world_from_camera_rotation(camera)
    starts_camera = (starts_world - center) @ world_from_camera
    ends_camera = (ends_world - center) @ world_from_camera
    projection = project_distorted_segments(starts_camera, ends_camera, camera_model)
    depth0 = projection.depth_starts
    depth1 = projection.depth_ends
    uv0 = projection.uv_starts
    uv1 = projection.uv_ends

    near = 0.05
    valid = (depth0 > near) & (depth1 > near)
    valid &= (depth0 <= radius) & (depth1 <= radius)
    valid &= np.isfinite(uv0).all(axis=1) & np.isfinite(uv1).all(axis=1)
    valid &= np.linalg.norm(uv1 - uv0, axis=1) <= max(width, height) * 0.45
    coordinate_limit = float(max(width, height) * 8)
    valid &= (np.abs(uv0) <= coordinate_limit).all(axis=1)
    valid &= (np.abs(uv1) <= coordinate_limit).all(axis=1)
    valid &= (
        (np.maximum(uv0[:, 0], uv1[:, 0]) >= -width)
        & (np.minimum(uv0[:, 0], uv1[:, 0]) <= 2 * width)
        & (np.maximum(uv0[:, 1], uv1[:, 1]) >= -height)
        & (np.minimum(uv0[:, 1], uv1[:, 1]) <= 2 * height)
    )

    if radius > float(fade_start_m):
        fade0 = np.clip((radius - depth0) / (radius - float(fade_start_m)), 0.0, 1.0)
        fade1 = np.clip((radius - depth1) / (radius - float(fade_start_m)), 0.0, 1.0)
        weights = 0.5 * (fade0 + fade1)
    else:
        weights = np.ones(len(depth0), dtype=np.float64)
    valid &= weights > 0.0
    indices = np.flatnonzero(valid)
    if not len(indices):
        return frame.copy()

    segments = np.rint(np.stack((uv0[indices], uv1[indices]), axis=1)).astype(np.int32)
    segments = segments.reshape(-1, 2, 1, 2)
    selected_colors = colors_value[indices]
    band_count = 12
    bands = np.clip(
        (weights[indices] * band_count - 1e-9).astype(np.int32),
        0,
        band_count - 1,
    )
    line_width = max(1, int(linewidth))
    color_layer = frame.copy()
    unique_colors, color_ids = np.unique(selected_colors, axis=0, return_inverse=True)
    for color_index, color in enumerate(unique_colors):
        chosen = segments[color_ids == color_index]
        if len(chosen):
            cv2.polylines(
                color_layer,
                chosen,
                False,
                tuple(int(value) for value in color),
                line_width,
                lineType=cv2.LINE_AA,
            )

    alpha_mask = np.zeros((height, width), dtype=np.uint8)
    for band in range(band_count):
        chosen = segments[bands == band]
        if len(chosen):
            cv2.polylines(
                alpha_mask,
                chosen,
                False,
                int(round((band + 1) / band_count * 255.0)),
                line_width,
                lineType=cv2.LINE_8,
            )
    active = alpha_mask > 0
    if not np.any(active):
        return frame.copy()
    result = frame.copy()
    alpha = alpha_mask[active].astype(np.float32)[:, None] / 255.0
    alpha *= float(np.clip(overlay_alpha, 0.0, 1.0))
    result[active] = (
        frame[active].astype(np.float32) * (1.0 - alpha)
        + color_layer[active].astype(np.float32) * alpha
    ).astype(np.uint8)
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_render_report(
    *,
    xml_path: Path,
    srt_path: Path,
    video_path: Path,
    cad_dir: Path,
    output_path: Path,
    source_hashes: Mapping[str, str],
    output_hash: str,
    start_frame: int,
    end_frame: int,
    sample_every: int,
    source_fps: float,
    rendered_frame_count: int,
    vertical_reference_m: float,
    camera_model: BentleyCameraModel,
) -> dict[str, object]:
    output_fps = float(source_fps) / int(sample_every)
    fx, fy = camera_model.focal_pixels
    return {
        "schema_version": "1.0",
        "position_source": "xml_adjusted_center",
        "orientation_source": "xml_pose_rotation",
        "position_interpolation": "ecef_linear",
        "orientation_interpolation": "shortest_arc_quaternion_slerp",
        "distortion_source": "xml_photogroup",
        "vertical_reference_source": "median_srt_abs_alt_minus_rel_alt",
        "vertical_reference_m": float(vertical_reference_m),
        "inputs": {
            "xml": str(xml_path),
            "srt": str(srt_path),
            "video": str(video_path),
            "cad_dir": str(cad_dir),
            "sha256": dict(source_hashes),
        },
        "source_interval": {
            "start_frame": int(start_frame),
            "end_frame": int(end_frame),
        },
        "sampled_source_frames": {
            "first_frame": int(start_frame),
            "last_frame": int(
                start_frame + max(0, rendered_frame_count - 1) * sample_every
            ),
            "count": int(rendered_frame_count),
        },
        "camera_model": {
            "type": "Perspective",
            "orientation": "XRightYDown",
            "image_size": list(camera_model.image_size),
            "focal_length_mm": float(camera_model.focal_length_mm),
            "sensor_size_mm": float(camera_model.sensor_size_mm),
            "focal_pixels": [float(fx), float(fy)],
            "horizontal_fov_deg": float(camera_model.horizontal_fov_deg),
            "principal_point_px": list(camera_model.principal_point_px),
            "distortion_k1_k2_k3_p1_p2": list(camera_model.distortion),
            "aspect_ratio": float(camera_model.aspect_ratio),
            "skew": float(camera_model.skew),
        },
        "render": {
            "output": str(output_path),
            "output_sha256": str(output_hash),
            "output_size": list(camera_model.image_size),
            "source_fps": float(source_fps),
            "output_fps": output_fps,
            "sample_every": int(sample_every),
            "rendered_frame_count": int(rendered_frame_count),
            "duration_sec": float(rendered_frame_count) / output_fps,
            "codec": "h264/libopenh264",
            "pixel_format": "yuv420p",
        },
    }


def _confirmed_georeference(central_meridian: float) -> CadGeoreference:
    return CadGeoreference(
        schema_version=1,
        horizontal_datum="CGCS2000",
        projection_family="gauss_kruger",
        zone_width_deg=3,
        central_meridian_deg=float(central_meridian),
        epsg=None,
        projected_axis_order="easting_northing",
        cad_axis_mapping="cad_x_easting_cad_y_northing",
        zone_prefix=False,
        linear_unit="metre",
        source="xml_at_reference_cli",
        confirmed=True,
        confidence=1.0,
        crs_source="custom",
    )


def _validate_input_paths(args: argparse.Namespace) -> None:
    for name in ("xml", "srt", "video", "cad_dir"):
        path = Path(getattr(args, name))
        if not path.exists():
            raise FileNotFoundError(f"{name} not found: {path}")


def _ffmpeg_command(
    executable: str,
    *,
    output_size: tuple[int, int],
    output_fps: float,
    output_path: Path,
) -> list[str]:
    width, height = output_size
    return [
        executable,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s:v",
        f"{width}x{height}",
        "-r",
        f"{output_fps:.12f}",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libopenh264",
        "-b:v",
        "8M",
        "-maxrate",
        "12M",
        "-bufsize",
        "16M",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def render_reference(args: argparse.Namespace) -> dict[str, object]:
    validate_render_interval(
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        sample_every=args.sample_every,
    )
    _validate_input_paths(args)
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    output_size = tuple(int(value) for value in args.output_size)
    if len(output_size) != 2 or min(output_size) <= 0:
        raise ValueError("output-size must contain two positive integers")

    camera_model = load_bentley_camera_model(args.xml)
    if output_size != camera_model.image_size:
        raise ValueError(
            "output-size must equal XML ImageDimensions to preserve exact calibration"
        )
    samples = load_bentley_pose_samples(args.xml)
    records = load_srt_records(args.srt)
    vertical_reference = srt_vertical_reference_m(records)
    georeference = _confirmed_georeference(args.central_meridian)
    sampled_frames = tuple(
        range(args.start_frame, args.end_frame + 1, args.sample_every)
    )
    poses = build_adjusted_at_track(
        samples,
        sampled_frames,
        georeference=georeference,
        cad_origin_xy=tuple(args.cad_origin),
        cad_scale=float(args.cad_scale),
        vertical_reference_m=vertical_reference,
        fov_deg=camera_model.horizontal_fov_deg,
    )

    cad = load_cad_bundle(args.cad_dir, origin_xy=(0.0, 0.0), cad_scale=1.0)
    starts, ends, colors = flatten_cad_segments(cad)
    print(f"[setup] XML samples={len(samples)} CAD segments={len(starts)}", flush=True)

    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise RuntimeError(f"video cannot be opened: {args.video}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    source_frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if source_fps <= 0.0 or source_frame_count <= 0:
        capture.release()
        raise RuntimeError("video has invalid FPS or frame count")
    if args.end_frame >= source_frame_count:
        capture.release()
        raise ValueError(
            f"end-frame {args.end_frame} exceeds video frame count {source_frame_count}"
        )
    output_fps = source_fps / args.sample_every
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        _ffmpeg_command(
            ffmpeg,
            output_size=output_size,
            output_fps=output_fps,
            output_path=output_path,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None:
        capture.release()
        process.kill()
        raise RuntimeError("FFmpeg stdin is unavailable")

    capture.set(cv2.CAP_PROP_POS_FRAMES, args.start_frame)
    current = args.start_frame
    rendered = 0
    pose_by_frame = {pose.frame_index: pose for pose in poses}
    try:
        while current <= args.end_frame:
            if (current - args.start_frame) % args.sample_every == 0:
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError(f"video decode failed at source frame {current}")
                frame = cv2.resize(frame, output_size, interpolation=cv2.INTER_AREA)
                pose = pose_by_frame[current]
                frame = render_calibrated_frame(
                    frame,
                    pose.camera,
                    starts,
                    ends,
                    camera_model,
                    colors_bgr=colors,
                    max_distance_m=float(args.max_distance_m),
                    fade_start_m=float(args.fade_start_m),
                    overlay_alpha=float(args.overlay_alpha),
                    linewidth=int(args.linewidth),
                )
                process.stdin.write(frame.tobytes())
                rendered += 1
                if rendered == 1 or rendered % 100 == 0 or rendered == len(poses):
                    print(
                        f"[render] {rendered}/{len(poses)} source_frame={current}",
                        flush=True,
                    )
            else:
                if not capture.grab():
                    raise RuntimeError(f"video decode failed at source frame {current}")
            current += 1
    finally:
        capture.release()
        process.stdin.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"FFmpeg exited with {return_code}: {stderr.strip()}")
    if rendered != len(poses):
        raise RuntimeError(f"rendered {rendered} frames but planned {len(poses)}")

    print("[report] hashing sources and output", flush=True)
    source_hashes = {
        "xml": _sha256(Path(args.xml)),
        "srt": _sha256(Path(args.srt)),
        "video": _sha256(Path(args.video)),
    }
    report = build_render_report(
        xml_path=Path(args.xml),
        srt_path=Path(args.srt),
        video_path=Path(args.video),
        cad_dir=Path(args.cad_dir),
        output_path=output_path,
        source_hashes=source_hashes,
        output_hash=_sha256(output_path),
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        sample_every=args.sample_every,
        source_fps=source_fps,
        rendered_frame_count=rendered,
        vertical_reference_m=vertical_reference,
        camera_model=camera_model,
    )
    report_path = output_path.parent / "render_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[done] {output_path}", flush=True)
    print(f"[done] {report_path}", flush=True)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        render_reference(args)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
