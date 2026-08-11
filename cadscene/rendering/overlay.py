from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

import cv2
import numpy as np

from cadscene.cad.loader import CadBundle, RoadLine, load_cad_bundle
from cadscene.cad.projection import style_alpha_for_distance, world_from_camera_rotation
from cadscene.core.camera import CameraState
from cadscene.core.io import ensure_dir, write_json, write_text
from cadscene.rendering.styles import style_for_kind


@dataclass(frozen=True)
class RenderOverlayConfig:
    video_path: str | Path
    cad_dir: str | Path
    sfm_camera_path: str | Path
    output_video: str | Path
    cad_scale: float | None = None
    origin_xy: tuple[float, float] | None = None
    debug_scale: float = 1.0
    overlay_linewidth: int = 3
    overlay_alpha: float = 0.88
    faded_overlay: bool = False
    max_distance_m: float | None = 900.0
    fade_start_m: float = 250.0
    start_frame: int | None = None
    end_frame: int | None = None
    sample_every: int = 1
    write_sample_frames: bool = False
    sample_frames_dir: str | Path | None = None


@dataclass(frozen=True)
class RenderOverlayResult:
    output_video: Path
    stats: dict
    warnings: list[str] = field(default_factory=list)


def load_camera_path_csv(path: str | Path) -> list[tuple[int, CameraState]]:
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"sfm_camera_path not found: {src}")
    rows: list[tuple[int, CameraState]] = []
    with src.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row:
                continue
            frame = int(float(row.get("frame_index", 0)))
            rows.append((frame, CameraState.from_row(row)))
    if not rows:
        raise ValueError(f"sfm_camera_path has no camera rows: {src}")
    rows.sort(key=lambda item: item[0])
    return rows


def _nearest_camera(frame_index: int, path_rows: Sequence[tuple[int, CameraState]]) -> CameraState:
    best = min(path_rows, key=lambda item: abs(item[0] - frame_index))
    return best[1]


def _cad_lines(bundle: CadBundle) -> list[RoadLine]:
    # 与旧 faded overlay 保持相同叠放顺序，中心线最后绘制。
    return [*bundle.refs, *bundle.edges, *bundle.centers]


def _densify_points_for_projection(points: np.ndarray, step_m: float) -> np.ndarray:
    """按旧版 20 CAD units 的间距插密，避免稀疏长段被投影裁剪切断。"""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return pts.copy()
    step = max(float(step_m), 1e-9)
    output = [pts[0]]
    for start, end in zip(pts[:-1], pts[1:]):
        segment = end - start
        count = max(1, int(np.ceil(float(np.linalg.norm(segment)) / step)))
        output.extend(start + segment * (index / count) for index in range(1, count + 1))
    return np.asarray(output, dtype=np.float64)


def _blend_line(base: np.ndarray, p0: tuple[int, int], p1: tuple[int, int], color: tuple[int, int, int], linewidth: int, alpha: float) -> None:
    overlay = base.copy()
    cv2.line(overlay, p0, p1, color, max(1, int(linewidth)), lineType=cv2.LINE_AA)
    cv2.addWeighted(overlay, float(alpha), base, 1.0 - float(alpha), 0, dst=base)


def render_frame_overlay(
    image_bgr: np.ndarray,
    camera: CameraState,
    cad: CadBundle,
    *,
    overlay_linewidth: int = 3,
    overlay_alpha: float = 0.88,
    faded_overlay: bool = False,
    max_distance_m: float | None = 900.0,
    fade_start_m: float = 250.0,
    cad_scale: float = 1.0,
    near_plane_m: float = 0.05,
) -> np.ndarray:
    height, width = image_bgr.shape[:2]
    color_layer = image_bgr.copy()
    # 与旧版一致：将远近淡出离散为少量 alpha 档位，最后一次混合，避免每线段复制整帧图像。
    band_count = 12
    band_runs: list[list[np.ndarray]] = [[] for _ in range(band_count)]
    band_weights = [(index + 1) / band_count for index in range(band_count)]
    rotation = world_from_camera_rotation(camera)
    center = np.asarray([camera.camera_x, camera.camera_y, camera.camera_z], dtype=np.float64)
    focal = float(width) / (2.0 * np.tan(np.radians(camera.fov_deg) / 2.0))
    max_jump = max(width, height) * 0.45
    # 旧版参数以原始 CAD 距离计，投影空间需按 cad_scale 折算。
    max_depth = (
        None
        if max_distance_m is None
        else float(max_distance_m) * float(cad_scale)
    )
    fade_start_depth = float(fade_start_m) * float(cad_scale)
    projection_step = 20.0 * float(cad_scale)

    for line in _cad_lines(cad):
        if len(line.points) < 2:
            continue
        style = style_for_kind(line.kind)
        line_color = line.color_bgr or style.color_bgr
        points = _densify_points_for_projection(line.points, projection_step)
        world = np.column_stack([points, np.zeros(len(points), dtype=np.float64)])
        camera_points = (world - center) @ rotation
        depth = camera_points[:, 2]
        valid = depth > near_plane_m
        if max_depth is not None:
            valid &= depth <= max_depth
        uv = np.empty((len(points), 2), dtype=np.float64)
        uv[:, 0] = width * 0.5 + camera_points[:, 0] * focal / np.maximum(depth, near_plane_m)
        uv[:, 1] = height * 0.5 + camera_points[:, 1] * focal / np.maximum(depth, near_plane_m)
        if (
            faded_overlay
            and max_depth is not None
            and max_depth > fade_start_depth
        ):
            fade_weight = np.clip((max_depth - depth) / (max_depth - fade_start_depth), 0.0, 1.0)
        else:
            fade_weight = np.ones(len(points), dtype=np.float64)
        linewidth = max(1, int(overlay_linewidth))
        finite = np.isfinite(uv).all(axis=1)
        jumps = np.linalg.norm(np.diff(uv, axis=0), axis=1)
        segment_valid = valid[:-1] & valid[1:] & finite[:-1] & finite[1:] & (jumps <= max_jump)
        segment_weight = 0.5 * (fade_weight[:-1] + fade_weight[1:])
        segment_valid &= segment_weight > 0
        segment_bands = np.clip((segment_weight * band_count - 1e-9).astype(np.int32), 0, band_count - 1)
        valid_indices = np.flatnonzero(segment_valid)
        if not len(valid_indices):
            continue
        boundaries = np.flatnonzero(
            (np.diff(valid_indices) != 1)
            | (segment_bands[valid_indices[1:]] != segment_bands[valid_indices[:-1]])
        ) + 1
        for run in np.split(valid_indices, boundaries):
            start_index = int(run[0])
            end_index = int(run[-1]) + 2
            polyline = np.rint(uv[start_index:end_index]).astype(np.int32).reshape(-1, 1, 2)
            band = int(segment_bands[start_index])
            cv2.polylines(color_layer, [polyline], False, line_color, linewidth, lineType=cv2.LINE_AA)
            band_runs[band].append(polyline)

    alpha_mask = np.zeros((height, width), dtype=np.uint8)
    for band, polylines in enumerate(band_runs):
        if polylines:
            value = int(round(band_weights[band] * 255.0))
            cv2.polylines(alpha_mask, polylines, False, value, overlay_linewidth, lineType=cv2.LINE_8)
    active = alpha_mask > 0
    if not np.any(active):
        return image_bgr.copy()
    blended = image_bgr.copy()
    weight = alpha_mask[active].astype(np.float32)[:, None] / 255.0
    weight *= float(np.clip(overlay_alpha, 0.0, 1.0))
    base_pixels = image_bgr[active].astype(np.float32)
    color_pixels = color_layer[active].astype(np.float32)
    blended[active] = (base_pixels * (1.0 - weight) + color_pixels * weight).astype(np.uint8)
    return blended


def _scale_frame(frame: np.ndarray, debug_scale: float) -> np.ndarray:
    scale = float(debug_scale)
    if abs(scale - 1.0) < 1e-9:
        return frame
    if scale <= 0:
        raise ValueError("debug_scale must be positive")
    return cv2.resize(frame, (max(1, int(round(frame.shape[1] * scale))), max(1, int(round(frame.shape[0] * scale)))))


def render_overlay_video(
    config: RenderOverlayConfig,
    *,
    progress_callback: Callable[[str, str, float], None] | None = None,
) -> RenderOverlayResult:
    video_path = Path(config.video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"video not found: {video_path}")
    path_rows = load_camera_path_csv(config.sfm_camera_path)
    cad = load_cad_bundle(config.cad_dir, origin_xy=config.origin_xy, cad_scale=config.cad_scale)
    lines = _cad_lines(cad)
    if not lines:
        raise ValueError(
            "no renderable CAD polyline found; expected road_center/road_edge/road_ref JSON "
            "or design.json with --cad-scale and --origin-xy"
        )
    warnings: list[str] = []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"video cannot be opened: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width0 = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height0 = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width0 <= 0 or height0 <= 0:
        raise RuntimeError(f"video has invalid size: {video_path}")

    out_path = Path(config.output_video)
    ensure_dir(out_path.parent)
    out_size = (max(1, int(round(width0 * config.debug_scale))), max(1, int(round(height0 * config.debug_scale))))
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"avc1"), fps, out_size)
    if not writer.isOpened():
        cap.release()
        raise RuntimeError(
            "H.264 output video cannot be opened for write; browser preview requires an H.264-capable OpenCV build"
        )

    sample_dir = Path(config.sample_frames_dir) if config.sample_frames_dir else out_path.parent / "sample_frames"
    if config.write_sample_frames:
        ensure_dir(sample_dir)

    start = max(0, int(config.start_frame or 0))
    end = int(config.end_frame) if config.end_frame is not None else frame_count - 1
    if start:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    rendered = 0
    current = start
    while current <= end:
        ok, frame = cap.read()
        if not ok:
            break
        if config.sample_every <= 1 or ((current - start) % max(1, int(config.sample_every)) == 0):
            frame = _scale_frame(frame, config.debug_scale)
            camera = _nearest_camera(current, path_rows)
            rendered_frame = render_frame_overlay(
                frame,
                camera,
                cad,
                overlay_linewidth=config.overlay_linewidth,
                overlay_alpha=config.overlay_alpha,
                faded_overlay=config.faded_overlay,
                max_distance_m=config.max_distance_m,
                fade_start_m=config.fade_start_m,
                cad_scale=float(config.cad_scale or 1.0),
            )
            writer.write(rendered_frame)
            if config.write_sample_frames and rendered < 5:
                cv2.imwrite(str(sample_dir / f"frame_{current:06d}.jpg"), rendered_frame)
            rendered += 1
            planned = max(1, ((end - start) // max(1, int(config.sample_every))) + 1)
            if rendered == 1 or rendered % 25 == 0 or current >= end:
                print(f"[render] frame {rendered}/{planned} source_frame={current}", flush=True)
                if progress_callback is not None:
                    progress_callback(
                        "rendering_frames",
                        f"rendered frame {rendered}/{planned}",
                        min(1.0, rendered / planned),
                    )
        current += 1

    cap.release()
    writer.release()
    stats = {
        "frame_count": int(frame_count),
        "rendered_frame_count": int(rendered),
        "width": int(out_size[0]),
        "height": int(out_size[1]),
        "fps": float(fps),
        "video_codec": "h264",
        "debug_scale": float(config.debug_scale),
        "overlay_linewidth": int(config.overlay_linewidth),
        "overlay_alpha": float(config.overlay_alpha),
        "faded_overlay": bool(config.faded_overlay),
        "max_distance_m": (
            None
            if config.max_distance_m is None
            else float(config.max_distance_m)
        ),
        "fade_start_m": float(config.fade_start_m),
        "camera_path_frame_count": int(len(path_rows)),
        "cad_polyline_count": int(len(lines)),
        "cad_coordinate_source": "design_json" if (Path(config.cad_dir) / "design.json").exists() else "road_json",
        "camera_interpolation": "nearest_frame",
        "warnings": warnings,
    }
    return RenderOverlayResult(output_video=out_path, stats=stats, warnings=warnings)


def build_render_report(*, inputs: Mapping[str, object], output_video: str | Path, stats: Mapping[str, object], warnings: Sequence[str]) -> str:
    warning_lines = [f"- {item}" for item in warnings] or ["- 无"]
    return "\n".join(
        [
            "# CAD 叠加渲染报告",
            "",
            "## 输入文件",
            "",
            f"- video: {inputs.get('video')}",
            f"- cad_dir: {inputs.get('cad_dir')}",
            f"- sfm_camera_path: {inputs.get('sfm_camera_path')}",
            "",
            "## 输出",
            "",
            f"- output_video: {output_video}",
            "",
            "## 渲染统计",
            "",
            f"- 渲染帧数: {stats.get('rendered_frame_count', 0)} / {stats.get('frame_count', 0)}",
            f"- 视频分辨率: {stats.get('width', 0)} x {stats.get('height', 0)}",
            f"- FPS: {float(stats.get('fps', 0.0)):.3f}",
            f"- CAD 线数量: {stats.get('cad_polyline_count', 0)}",
            f"- camera path 帧数: {stats.get('camera_path_frame_count', 0)}",
            f"- 缺帧策略: {stats.get('camera_interpolation', 'nearest_frame')}",
            "",
            "## Overlay 参数",
            "",
            f"- debug_scale: {stats.get('debug_scale')}",
            f"- overlay_linewidth: {stats.get('overlay_linewidth')}",
            f"- overlay_alpha: {stats.get('overlay_alpha')}",
            f"- faded_overlay: {stats.get('faded_overlay')}",
            f"- max_distance_m: {stats.get('max_distance_m')}",
            f"- fade_start_m: {stats.get('fade_start_m')}",
            "",
            "## Warning",
            "",
            *warning_lines,
            "",
            "## 声明",
            "",
            "本阶段只渲染已经对齐好的 camera path，不修改相机轨迹，不做 refine，不做 semantic refine。",
            "",
        ]
    )


def write_render_outputs(stage_dir: str | Path, result: RenderOverlayResult, *, inputs: Mapping[str, object]) -> None:
    stage = ensure_dir(stage_dir)
    write_json(stage / "render_stats.json", result.stats)
    write_text(stage / "render_report.md", build_render_report(inputs=inputs, output_video=result.output_video, stats=result.stats, warnings=result.warnings))
