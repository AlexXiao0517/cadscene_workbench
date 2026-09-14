"""Build an SRT-locked CAD route with visual-only camera attitudes."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
from dataclasses import replace
from hashlib import sha256
from time import perf_counter, sleep
from typing import Mapping, Sequence

import numpy as np

from cadscene.core.camera import (
    CameraState,
    decompose_world_from_camera_rotation,
    rotation_matrix_to_quaternion_wxyz,
)
from cadscene.core.coordinates import cad_meters_to_web_camera, python_state_to_web_camera
from cadscene.sfm.pointcloud import load_ply, sample_indices
from cadscene.sfm.trajectory import load_sfm_trajectory
from cadscene.srt.colmap_pose_transfer import (
    orientation_solution_from_colmap_transfer,
    transfer_colmap_pose_to_srt,
)
from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackPosition,
    FixedTrackVisualPoseConfig,
    OrientationSolution,
    build_fixed_track_positions,
)
from cadscene.srt.full_pose import horizontal_fov_intrinsics
from cadscene.srt.parser import load_srt_records
from cadscene.dji.metadata import load_dji_pose_priors
from cadscene.srt.joint_pose_alignment import solve_joint_alignment
from cadscene.srt.georeference import cad_raw_to_local_m, project_wgs84_to_cad_raw
from cadscene.terrain.context import build_terrain_context, write_terrain_context
from cadscene.terrain.tpkg import (
    load_tpkg,
    merge_terrain_controls,
    sampled_control_points,
)


STAGES = (
    ("parse_srt", "正在解析 SRT", 0.08),
    ("project_track", "正在将 SRT 轨迹投影到 CAD", 0.22),
    ("load_reconstruction", "正在读取 COLMAP 稀疏重建", 0.42),
    ("align_orientation", "正在将重建姿态配准到 SRT 轨迹", 0.82),
    ("publish_workbench", "正在准备轨迹工作台", 0.95),
    ("completed", "SRT 轨迹与重建姿态已生成", 1.0),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 SRT→CAD 固定轨迹并从 COLMAP 稀疏重建转移相机姿态。"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--frame-map", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--reconstruction-trajectory", required=True, type=Path)
    parser.add_argument("--sparse-ply", type=Path)
    parser.add_argument("--progress-file", type=Path)
    return parser


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def _atomic_write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ValueError("fixed route contains no rows")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(
            handle, "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_progress(
    path: Path | None,
    stage: str,
    message: str,
    fraction: float,
) -> None:
    if path is None:
        return
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "message": message,
        "fraction": fraction,
    }
    for attempt in range(5):
        try:
            _atomic_write_json(path, payload)
            return
        except PermissionError:
            if attempt == 4:
                return
            sleep(0.01 * (2**attempt))


def _video_metadata(value: object) -> tuple[int, int, float]:
    if not isinstance(value, Mapping):
        raise ValueError("fixed-track configuration requires video_metadata")
    try:
        width = int(value["width"])
        height = int(value["height"])
        fps = float(value["fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("video_metadata requires width, height and fps") from exc
    if width <= 0 or height <= 0 or not np.isfinite(fps) or fps <= 0.0:
        raise ValueError("video_metadata width, height and fps must be positive")
    return width, height, fps


def _source_calibration(reconstruction, source_size: tuple[int, int]) -> dict[str, object]:
    intrinsics = dict(reconstruction.intrinsics)
    model = str(intrinsics.get("model", "")).upper()
    params = [float(value) for value in intrinsics.get("params", [])]
    solve_width = int(intrinsics.get("width", reconstruction.width))
    solve_height = int(intrinsics.get("height", reconstruction.height))
    source_width, source_height = source_size
    if solve_width <= 0 or solve_height <= 0:
        raise ValueError("COLMAP calibration has invalid solve dimensions")
    scale_x = source_width / solve_width
    scale_y = source_height / solve_height
    if model == "RADIAL" and len(params) >= 5:
        focal, cx, cy, k1, k2 = params[:5]
    elif model == "PINHOLE" and len(params) >= 4:
        fx, fy, cx, cy = params[:4]
        focal = 0.5 * (fx * scale_x / scale_y + fy)
        k1 = k2 = 0.0
    else:
        raise ValueError(f"unsupported COLMAP calibration for fixed-track rendering: {model}")
    calibration = {
        "schema_version": "cadscene_camera_calibration_v1",
        "model": "RADIAL",
        "source_size": [source_width, source_height],
        "solve_size": [solve_width, solve_height],
        "focal_px": focal * scale_x,
        "cx_px": cx * scale_x,
        "cy_px": cy * scale_y,
        "k1": k1,
        "k2": k2,
        "calibration_source": "COLMAP_RADIAL_self_calibration" if model == "RADIAL" else "legacy_PINHOLE_promoted",
    }
    values = [calibration[key] for key in ("focal_px", "cx_px", "cy_px", "k1", "k2")]
    if not np.all(np.isfinite(np.asarray(values, dtype=np.float64))) or float(calibration["focal_px"]) <= 0:
        raise ValueError("COLMAP calibration contains invalid values")
    return calibration


def _intrinsics_from_calibration(calibration: Mapping[str, object]) -> dict[str, object]:
    width, height = calibration["source_size"]  # type: ignore[misc]
    return {
        "model": "RADIAL",
        "width": int(width),
        "height": int(height),
        "params": [
            float(calibration["focal_px"]),
            float(calibration["cx_px"]),
            float(calibration["cy_px"]),
            float(calibration["k1"]),
            float(calibration["k2"]),
        ],
    }


def _compile_terrain(
    payload: Mapping[str, object],
    config: FixedTrackVisualPoseConfig,
    positions: Sequence[FixedTrackPosition],
):
    paths = payload.get("terrain_source_paths", ())
    fingerprints = payload.get("terrain_source_fingerprints", ())
    if not isinstance(paths, list) or not isinstance(fingerprints, list):
        paths, fingerprints = [], []
    expected = {
        str(path): str(fingerprint)
        for path, fingerprint in zip(paths, fingerprints)
    }
    warnings: list[str] = []
    items = []

    def project(longitude: float, latitude: float) -> tuple[float, float]:
        raw = project_wgs84_to_cad_raw(longitude, latitude, config.georeference)
        return cad_raw_to_local_m(raw, config.cad_origin_xy, config.cad_scale)

    for value in paths:
        path = Path(str(value))
        try:
            controls = load_tpkg(path, project_lon_lat=project)
            wanted = expected.get(str(value))
            actual = controls.sources[0].source_id if controls.sources else ""
            if wanted and wanted != actual:
                warnings.append(f"高程文件指纹已变化，已排除：{path.name}")
                continue
            items.append(controls)
        except (OSError, ValueError) as exc:
            warnings.append(f"高程文件无效，已排除 {path.name}: {exc}")
    controls = merge_terrain_controls(items) if items else None
    georef_json = json.dumps(
        config.georeference.to_dict(), sort_keys=True, separators=(",", ":")
    )
    context = build_terrain_context(
        controls,
        np.asarray([item.center[:2] for item in positions], dtype=np.float64),
        route_time_sec=tuple(item.pts_time_sec for item in positions),
        cad_fingerprint=str(payload.get("cad_asset_fingerprint") or "unbound"),
        georeference_fingerprint=sha256(georef_json.encode("utf-8")).hexdigest(),
        warnings=warnings,
    )
    return context, controls


def _align_relative_height_datum(
    positions: Sequence[FixedTrackPosition],
    controls,
    terrain_context,
) -> tuple[tuple[FixedTrackPosition, ...], object]:
    from cadscene.terrain.height_reference import select_height_reference

    context = select_height_reference(terrain_context, controls, [p.abs_alt for p in positions])
    if context.camera_height_datum_source != "srt_abs_alt_unverified":
        return tuple(positions), context
    return tuple(
        replace(
            p,
            canonical_center=(*p.canonical_center[:2], float(p.abs_alt)),
            center=(*p.center[:2], float(p.abs_alt) + p.center[2] - p.canonical_center[2]),
            height_source="abs_alt",
        ) for p in positions
    ), context


def _require_complete_render_path(
    positions: Sequence[FixedTrackPosition],
    solution: OrientationSolution,
    frame_map: Mapping[str, object],
    clip_id: str,
) -> None:
    clips = frame_map.get("clips")
    if not isinstance(clips, list):
        raise ValueError("exact frame map requires a clips list")
    selected = next(
        (
            item
            for item in clips
            if isinstance(item, Mapping) and str(item.get("clip_id")) == clip_id
        ),
        clips[0] if len(clips) == 1 and isinstance(clips[0], Mapping) else None,
    )
    frames = selected.get("frames") if isinstance(selected, Mapping) else None
    if not isinstance(frames, list) or not frames:
        raise ValueError("exact frame map contains no authoritative frames")
    expected = set(range(len(frames)))
    position_frames = {int(item.frame_index) for item in positions}
    orientation_frames = {int(frame) for frame in solution.rotations}
    missing_positions = sorted(expected - position_frames)
    missing_orientations = sorted(expected - orientation_frames)
    if missing_positions or missing_orientations:
        raise ValueError(
            "complete per-frame SRT position and COLMAP pose are required before "
            "entering the workbench: "
            f"missing_positions={len(missing_positions)}, "
            f"missing_orientations={len(missing_orientations)}"
        )


def _camera_values(
    position: FixedTrackPosition,
    solution: OrientationSolution,
    config: FixedTrackVisualPoseConfig,
) -> tuple[list[float], float, float, float, bool]:
    rotation = solution.rotations.get(position.frame_index)
    if rotation is None:
        return [1.0, 0.0, 0.0, 0.0], 0.0, 0.0, 0.0, False
    camera_from_world = np.asarray(rotation, dtype=np.float64)
    yaw, pitch, roll = decompose_world_from_camera_rotation(
        camera_from_world.T
    )
    return (
        rotation_matrix_to_quaternion_wxyz(camera_from_world),
        float(yaw),
        float(pitch),
        float(roll),
        True,
    )


def _build_payloads(
    *,
    dataset: str,
    run_id: str,
    positions: Sequence[FixedTrackPosition],
    solution: OrientationSolution,
    config: FixedTrackVisualPoseConfig,
    intrinsics: Mapping[str, object],
    video_metadata: tuple[int, int, float],
    reconstruction_image_size: tuple[int, int],
    phase_timings_seconds: Mapping[str, float],
    diagnostic_points: Mapping[str, object] | None = None,
    terrain_context: Mapping[str, object],
) -> dict[str, object]:
    width, height, fps = video_metadata
    height_source = positions[0].height_source if positions else "rel_alt"
    poses: list[dict[str, object]] = []
    path_rows: list[dict[str, object]] = []
    keyframes: list[dict[str, object]] = []
    viewer_track: list[dict[str, object]] = []
    camera_params = [float(value) for value in intrinsics.get("params", ())]
    solve_metadata = {
        "reconstruction_resolution": config.reconstruction_resolution,
        "camera_model": str(intrinsics.get("model", "RADIAL")),
        "camera_focal_px": camera_params[0],
        "camera_principal_point_px": camera_params[1:3],
        "camera_radial_distortion": camera_params[3:5],
        "source_video_size": [width, height],
        "reconstruction_image_size": [
            int(reconstruction_image_size[0]),
            int(reconstruction_image_size[1]),
        ],
    }
    relative_count = len(solution.relative_rotations)
    relative_coverage = relative_count / max(1, len(positions))
    relative_status = (
        "relative_ready"
        if relative_coverage >= 0.8
        else "relative_partial"
        if relative_count
        else "unavailable"
    )
    recommended_position = next(
        (
            item
            for item in positions
            if item.frame_index == solution.recommended_anchor_frame
        ),
        None,
    )
    recommendation_meta: dict[str, object] = {
        "relative_orientation_status": relative_status,
        "relative_orientation_count": relative_count,
        "relative_orientation_coverage": relative_coverage,
        "recommended_anchor_frame": (
            recommended_position.frame_index if recommended_position else None
        ),
        "recommended_anchor_source_pts": (
            recommended_position.source_pts if recommended_position else None
        ),
        "recommended_anchor_time_sec": (
            recommended_position.pts_time_sec if recommended_position else None
        ),
    }
    for position in positions:
        quaternion, yaw, pitch, roll, orientation_available = _camera_values(
            position, solution, config
        )
        center = [float(value) for value in position.center]
        pose: dict[str, object] = {
                "frame_index": position.frame_index,
                "source_pts": position.source_pts,
                "pts_time_sec": position.pts_time_sec,
                "registered": orientation_available,
                "position_available": True,
                "orientation_available": orientation_available,
                "center": center,
                "canonical_center": list(position.canonical_center),
                "srt_interpolated": position.interpolated,
                "latitude": position.latitude,
                "longitude": position.longitude,
                "rel_alt": position.rel_alt,
                "abs_alt_diagnostic": position.abs_alt,
                "height_source": position.height_source,
                "projected_easting": position.projected_easting,
                "projected_northing": position.projected_northing,
                "cad_raw_x": position.cad_raw_x,
                "cad_raw_y": position.cad_raw_y,
            }
        if orientation_available:
            pose["cam_from_world_quat_wxyz"] = quaternion
        relative_rotation = solution.relative_rotations.get(position.frame_index)
        component_id = solution.component_ids.get(position.frame_index)
        if relative_rotation is not None and component_id is not None:
            pose["visual_component_id"] = int(component_id)
            pose["cam_from_visual_local_quat_wxyz"] = (
                rotation_matrix_to_quaternion_wxyz(relative_rotation)
            )
        poses.append(pose)
        path_rows.append(
            {
                "frame_index": position.frame_index,
                "source_pts": position.source_pts,
                "pts_time_sec": position.pts_time_sec,
                "camera_x": center[0],
                "camera_y": center[1],
                "camera_z": center[2],
                "yaw": yaw,
                "pitch": pitch,
                "roll": roll,
                "fov": config.horizontal_fov_deg,
                "path_source": "srt_cad_locked",
                "position_available": True,
                "orientation_available": orientation_available,
                "relative_orientation_available": relative_rotation is not None,
                "visual_component_id": component_id,
                "status": "ok",
            }
        )
        camera = python_state_to_web_camera(
            CameraState(
                camera_x=center[0],
                camera_y=center[1],
                camera_z=center[2],
                yaw_deg=yaw,
                pitch_deg=pitch,
                roll_deg=roll,
                fov_deg=config.horizontal_fov_deg,
                cad_scale=config.cad_scale,
            ),
            config.cad_origin_xy,
        )
        if orientation_available:
            keyframes.append(
                {
                    "frame": position.frame_index,
                    "time": position.pts_time_sec,
                    "source": "algorithm_prediction",
                    "position_source": "srt_cad_locked",
                    "orientation_available": True,
                    "camera": camera,
                }
            )
        viewer_track.append(
            {
                "frame_index": position.frame_index,
                "source": "metric_direct_srt",
                "position_source": "srt_cad_locked",
                "orientation_available": orientation_available,
                "relative_orientation_available": relative_rotation is not None,
                "visual_component_id": component_id,
                "camera": camera,
            }
        )
    trajectory = {
        "fps": fps,
        "width": width,
        "height": height,
        "video_width": width,
        "video_height": height,
        "intrinsics": [dict(intrinsics)],
        "poses": poses,
        "meta": {
            "workflow": "srt_fixed_track_visual_pose",
            "trajectory_mode": "srt_fixed_track_visual_pose",
            "trajectory_status": solution.status,
            "coordinate_system": "cad_local_m",
            "metric_scale_locked": True,
            "position_source": "srt_cad_locked",
            "pose_prior_schema": "srt_pose_prior_v1",
            "edit_policy": "six_dof_keyframe_residuals",
            "orientation_source": "colmap_sparse_srt_aligned",
            "orientation_status": solution.status,
            "point_cloud_generated": bool(
                diagnostic_points and int(diagnostic_points.get("count_exported", 0))
            ),
            "horizontal_datum": "CGCS2000",
            "georeference": config.georeference.to_dict(),
            "cad_origin_xy": list(config.cad_origin_xy),
            "cad_scale": config.cad_scale,
            "route_offset_xyz_m": list(config.route_offset_xyz_m),
            "height_source": height_source,
            "absolute_height_usage": "camera_z" if height_source == "abs_alt" else "diagnostic_only",
            "horizontal_fov_deg": config.horizontal_fov_deg,
            "fov_source": "user",
            "source_time_base": {
                "numerator": config.source_time_base.numerator,
                "denominator": config.source_time_base.denominator,
            },
            "source_start_pts": config.source_start_pts,
            "source_end_pts_exclusive": config.source_end_pts_exclusive,
            "warnings": list(solution.warnings),
            "position_count": len(positions),
            "orientation_count": len(solution.rotations),
            "orientation_coverage": len(solution.rotations) / max(1, len(positions)),
            **solve_metadata,
            **recommendation_meta,
            **terrain_context,
        },
    }
    track = {
        "schema_version": "cadscene_camera_track_pred_v1",
        "fps": fps,
        "keyframes": keyframes,
        "meta": {
            "generated_by": "cadscene.build_srt_fixed_track_visual_pose",
            "coordinate_system": "web_cad_world",
            "workflow": "srt_fixed_track_visual_pose",
            "pose_prior_schema": "srt_pose_prior_v1",
            "metric_scale_locked": True,
            "position_source": "srt_cad_locked",
            "orientation_source": "colmap_sparse_srt_aligned",
            "edit_policy": "six_dof_keyframe_residuals",
            "orientation_status": solution.status,
            **solve_metadata,
            **recommendation_meta,
            **terrain_context,
        },
    }
    empty_bbox = {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    scene = {
        "schema_version": "cadscene_sfm_viewer_scene_v1",
        "meta": {
            "generated_by": "cadscene.build_srt_fixed_track_visual_pose",
            "coordinate_system": "web_cad_world",
            "point_transform": "none",
            "global_track_transform": "metric_direct_srt",
            "anchored_track_transform": "none",
            "pitch_convention": "frontend_pitch_negated_from_python_pitch",
            "dataset": dataset,
            "run_id": run_id,
            "rgb_range": "0_255",
            "workflow": "srt_fixed_track_visual_pose",
            "pose_prior_schema": "srt_pose_prior_v1",
            "metric_scale_locked": True,
            "position_source": "srt_cad_locked",
            "orientation_source": "colmap_sparse_srt_aligned",
            "edit_policy": "six_dof_keyframe_residuals",
            "orientation_status": solution.status,
            "point_cloud_generated": bool(
                diagnostic_points and int(diagnostic_points.get("count_exported", 0))
            ),
            **solve_metadata,
            **recommendation_meta,
            **terrain_context,
        },
        "points": dict(diagnostic_points) if diagnostic_points else {
            "count_original": 0,
            "count_exported": 0,
            "sample_mode": "none",
            "voxel_size": 0.0,
            "has_rgb": False,
            "rgb_range": "0_255",
            "bbox": empty_bbox,
            "data": [],
        },
        "tracks": {
            "global_sfm_track": viewer_track,
            "anchored_camera_path": [],
        },
        "suggestions": [],
        "quality": {
            "available": False,
            "quality_timeline_ref": "",
            "suggestions_ref": "",
        },
        "warnings": [
            *solution.warnings,
            *(
                str(item)
                for item in terrain_context.get("terrain_warnings", ())
            ),
        ],
    }
    diagnostics = {
        "schema_version": 1,
        "clip_id": run_id,
        "workflow": "srt_fixed_track_visual_pose",
        "position_source": "srt_cad_locked",
        "position_count": len(positions),
        "orientation_count": len(solution.rotations),
        "orientation_coverage": len(solution.rotations) / max(1, len(positions)),
        "orientation_status": solution.status,
        "point_cloud_generated": bool(
            diagnostic_points and int(diagnostic_points.get("count_exported", 0))
        ),
        **solve_metadata,
        **recommendation_meta,
        **terrain_context,
        "route_offset_xyz_m": list(config.route_offset_xyz_m),
        "height_source": height_source,
        "abs_alt_usage": "camera_z" if height_source == "abs_alt" else "diagnostic_only",
        "reconstruction_alignment": [dict(item) for item in solution.diagnostics],
        "warnings": list(solution.warnings),
        "georeference": config.georeference.to_dict(),
        "phase_timings_seconds": {
            key: float(value) for key, value in phase_timings_seconds.items()
        },
    }
    timings_report = "\n".join(
        f"  - {key}: {float(value):.6f} 秒"
        for key, value in phase_timings_seconds.items()
    )
    report = (
        "# SRT 固定轨迹 + 稀疏重建姿态报告\n\n"
        f"- 轨迹帧数：{len(positions)}\n"
        f"- 可用姿态帧数：{len(solution.rotations)}\n"
        f"- 姿态状态：{solution.status}\n"
        f"- 重建姿态覆盖：{relative_status}（{relative_count}/{len(positions)}）\n"
        f"- 推荐姿态锚点帧：{solution.recommended_anchor_frame}\n"
        f"- 水平 FOV：{config.horizontal_fov_deg:g}°（用户输入）\n"
        f"- 姿态解算档位：{config.reconstruction_resolution}\n"
        f"- 源视频尺寸：{width}×{height}\n"
        f"- 实际姿态解算尺寸：{reconstruction_image_size[0]}×{reconstruction_image_size[1]}\n"
        f"- 整条路线统一偏移：{list(config.route_offset_xyz_m)} 米\n"
        "- 姿态来源：COLMAP 稀疏三维重建配准到 SRT/CAD 坐标。\n"
        "- 位置来源：SRT 经已确认的 CGCS2000 参数投影到 CAD；最终相机中心逐帧强制采用 SRT。\n"
        f"- 高度来源：{height_source}；有可用地形及完整绝对高度时，以 abs_alt 约束相机 Z。\n"
        f"- 诊断点云：{int((diagnostic_points or {}).get('count_exported', 0))} 点，可关闭且不参与渲染门禁。\n"
        "- 性能说明：缺失姿态路线运行真实稀疏重建，耗时可能接近普通 SfM；跳过稠密重建和独立质量检测。\n"
        "- 阶段耗时：\n"
        f"{timings_report}\n"
    )
    return {
        "trajectory": trajectory,
        "path_rows": path_rows,
        "diagnostics": diagnostics,
        "report": report,
        "track": track,
        "scene": scene,
    }


def _build_diagnostic_points(
    sparse_ply: Path | None,
    sim3,
    config: FixedTrackVisualPoseConfig,
    *,
    max_points: int = 80_000,
) -> dict[str, object] | None:
    if sparse_ply is None:
        return None
    points, colors = load_ply(sparse_ply)
    selected = sample_indices(len(points), max_points, mode="uniform")
    points_cad = sim3.apply(points[selected])
    selected_colors = colors[selected] if colors is not None else None
    data: list[list[float | int]] = []
    for index, point in enumerate(points_cad):
        web = cad_meters_to_web_camera(point, config.cad_origin_xy, config.cad_scale)
        row: list[float | int] = [
            round(float(web["x"]), 6),
            round(float(web["y"]), 6),
            round(float(web["z"]), 6),
        ]
        if selected_colors is not None:
            row.extend(int(value) for value in selected_colors[index])
        data.append(row)
    coordinates = np.asarray(
        [row[:3] for row in data], dtype=np.float64
    ).reshape(-1, 3)
    bbox = (
        {
            "min": coordinates.min(axis=0).round(6).tolist(),
            "max": coordinates.max(axis=0).round(6).tolist(),
        }
        if len(coordinates)
        else {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    )
    return {
        "count_original": int(len(points)),
        "count_exported": int(len(data)),
        "sample_mode": "uniform",
        "voxel_size": 0.0,
        "has_rgb": selected_colors is not None,
        "rgb_range": "0_255",
        "bbox": bbox,
        "data": data,
    }


def _publish_payloads(staging_root: Path, payloads: Mapping[str, object]) -> None:
    trajectory_dir = staging_root / "02_srt_visual_pose"
    alignment_dir = staging_root / "03_alignment"
    scene_dir = staging_root / "05_viewer_scene"
    _atomic_write_json(
        trajectory_dir / "camera_trajectory_visual_pose.json",
        payloads["trajectory"],
    )
    _atomic_write_csv(
        trajectory_dir / "camera_path_srt_locked.csv",
        payloads["path_rows"],  # type: ignore[arg-type]
    )
    _atomic_write_json(
        trajectory_dir / "orientation_diagnostics.json",
        payloads["diagnostics"],
    )
    _atomic_write_json(
        trajectory_dir / "camera_calibration.json",
        payloads["calibration"],
    )
    _atomic_write_json(
        trajectory_dir / "joint_alignment.json",
        payloads["joint_alignment"],
    )
    _atomic_write_text(
        trajectory_dir / "visual_pose_report.md",
        str(payloads["report"]),
    )
    _atomic_write_json(alignment_dir / "camera_track_pred.json", payloads["track"])
    _atomic_write_json(scene_dir / "sfm_viewer_scene.json", payloads["scene"])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_root = args.output_root / args.dataset / args.run_id
    staging_root: Path | None = None
    try:
        for path, label in (
            (args.video, "video"),
            (args.srt, "SRT"),
            (args.frame_map, "frame map"),
            (args.config, "configuration"),
            (args.reconstruction_trajectory, "COLMAP trajectory"),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"physical {label} input is missing: {path}")
        parse_started = perf_counter()
        payload = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise ValueError("fixed-track configuration must be an object")
        build_payload = payload.get("build")
        if not isinstance(build_payload, Mapping):
            raise ValueError("fixed-track configuration requires a build object")
        config = FixedTrackVisualPoseConfig.from_dict(build_payload)
        if config.clip_id != args.run_id:
            raise ValueError("configuration clip_id must match --run-id")
        metadata = _video_metadata(payload.get("video_metadata"))
        _write_progress(args.progress_file, *STAGES[0])
        records = load_srt_records(args.srt)
        frame_map = json.loads(args.frame_map.read_text(encoding="utf-8-sig"))
        parse_elapsed = perf_counter() - parse_started
        _write_progress(args.progress_file, *STAGES[1])
        project_started = perf_counter()
        positions = build_fixed_track_positions(records, frame_map, config)
        terrain_context, terrain_controls = _compile_terrain(
            payload, config, positions
        )
        positions, terrain_context = _align_relative_height_datum(
            positions, terrain_controls, terrain_context
        )
        project_elapsed = perf_counter() - project_started
        _write_progress(args.progress_file, *STAGES[2])
        transfer_started = perf_counter()
        reconstruction = load_sfm_trajectory(args.reconstruction_trajectory)
        transfer = transfer_colmap_pose_to_srt(reconstruction, positions)
        calibration = _source_calibration(reconstruction, (metadata[0], metadata[1]))
        paired_frames = np.asarray(transfer.registered_frames, dtype=np.int64)
        by_frame = {item.frame_index: item for item in positions}
        dji = load_dji_pose_priors(args.video, paired_frames)
        joint_payload: dict[str, object] = {
            "schema_version": "cadscene_joint_alignment_v1",
            "position_constraint": "SRT projected CAD-local",
            "dji_prior_available": dji.available,
            "dji_prior_reason": dji.reason,
            "dji_packet_count": dji.packet_count,
            "status": "fallback_colmap_srt",
        }
        if len(paired_frames) >= 3:
            try:
                joint = solve_joint_alignment(
                    frames=paired_frames,
                    colmap_centers=np.asarray(
                        [reconstruction.center_at(int(frame)) for frame in paired_frames]
                    ),
                    colmap_world_from_camera=np.asarray(
                        [reconstruction.orientation_at(int(frame)).T for frame in paired_frames]
                    ),
                    srt_centers=np.asarray(
                        [by_frame[int(frame)].center for frame in paired_frames]
                    ),
                    dji_world_from_camera=(dji.world_from_camera if dji.available else None),
                    frame_rate=metadata[2],
                )
                if joint.success:
                    transfer = replace(
                        transfer,
                        world_from_camera={
                            int(frame): joint.world_from_camera[index]
                            for index, frame in enumerate(paired_frames)
                        },
                    )
                joint_payload.update(
                    {
                        "status": "success" if joint.success else "failed",
                        "message": joint.message,
                        "scale": joint.scale,
                        "world_rotation": joint.world_rotation.tolist(),
                        "translation": joint.translation.tolist(),
                        "knot_times_sec": joint.knot_times_sec.tolist(),
                        "knot_offsets_m": joint.knot_offsets_m.tolist(),
                        "initial_cost": joint.initial_cost,
                        "final_cost": joint.final_cost,
                        "nfev": joint.nfev,
                    }
                )
            except (ValueError, RuntimeError) as exc:
                joint_payload["message"] = str(exc)
        solution = orientation_solution_from_colmap_transfer(
            transfer,
            positions,
            max_interpolation_gap_sec=config.max_orientation_interpolation_gap_sec,
        )
        _require_complete_render_path(positions, solution, frame_map, config.clip_id)
        diagnostic_points = _build_diagnostic_points(
            args.sparse_ply,
            transfer.sim3,
            config,
        )
        transfer_elapsed = perf_counter() - transfer_started
        _write_progress(args.progress_file, *STAGES[3])
        payloads = _build_payloads(
            dataset=args.dataset,
            run_id=args.run_id,
            positions=positions,
            solution=solution,
            config=config,
            intrinsics=_intrinsics_from_calibration(calibration),
            video_metadata=metadata,
            reconstruction_image_size=(
                int(reconstruction.width), int(reconstruction.height)
            ),
            phase_timings_seconds={
                "parse_inputs": parse_elapsed,
                "project_srt_track": project_elapsed,
                "transfer_colmap_attitude": transfer_elapsed,
            },
            diagnostic_points=diagnostic_points,
            terrain_context=terrain_context.to_dict(),
        )
        payloads["calibration"] = calibration
        payloads["joint_alignment"] = joint_payload
        run_root.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".srt-fixed-track-", dir=run_root)
        )
        _publish_payloads(staging_root, payloads)
        write_terrain_context(
            staging_root / "02_srt_visual_pose",
            terrain_context,
            terrain_controls,
        )
        _write_progress(args.progress_file, *STAGES[4])
        target_names = ("02_srt_visual_pose", "03_alignment", "05_viewer_scene")
        occupied = [name for name in target_names if (run_root / name).exists()]
        if occupied:
            raise FileExistsError(
                "fixed-track output already exists: " + ", ".join(occupied)
            )
        for name in target_names:
            os.replace(staging_root / name, run_root / name)
        _write_progress(args.progress_file, *STAGES[5])
        print(f"输出路径: {run_root / '02_srt_visual_pose'}")
        return 0
    except Exception as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
