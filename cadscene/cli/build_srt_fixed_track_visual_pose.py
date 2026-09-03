"""Build an SRT-locked CAD route with visual-only camera attitudes."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
from time import perf_counter, sleep
from typing import Mapping, Sequence

import numpy as np

from cadscene.core.camera import (
    CameraState,
    decompose_world_from_camera_rotation,
    rotation_matrix_to_quaternion_wxyz,
)
from cadscene.core.coordinates import python_state_to_web_camera
from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackPosition,
    FixedTrackVisualPoseConfig,
    OrientationSolution,
    build_fixed_track_positions,
    estimate_video_orientations,
)
from cadscene.srt.full_pose import horizontal_fov_intrinsics
from cadscene.srt.parser import load_srt_records


STAGES = (
    ("parse_srt", "正在解析 SRT", 0.08),
    ("project_track", "正在将 SRT 轨迹投影到 CAD", 0.22),
    ("extract_visual_constraints", "正在提取视觉姿态约束", 0.55),
    ("solve_orientation", "正在估计固定轨迹上的相机姿态", 0.82),
    ("publish_workbench", "正在准备轨迹工作台", 0.95),
    ("completed", "SRT 轨迹与视觉姿态已生成", 1.0),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 SRT→CAD 固定轨迹并仅从视频估计相机姿态。"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--frame-map", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
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
    phase_timings_seconds: Mapping[str, float],
) -> dict[str, object]:
    width, height, fps = video_metadata
    poses: list[dict[str, object]] = []
    path_rows: list[dict[str, object]] = []
    keyframes: list[dict[str, object]] = []
    viewer_track: list[dict[str, object]] = []
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
                    "position_locked": True,
                    "orientation_available": True,
                    "camera": camera,
                }
            )
        viewer_track.append(
            {
                "frame_index": position.frame_index,
                "source": "metric_direct_srt",
                "position_source": "srt_cad_locked",
                "position_locked": True,
                "orientation_available": orientation_available,
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
            "position_edit_policy": "uniform_xyz_offset_only",
            "orientation_source": "visual_fixed_center",
            "orientation_status": solution.status,
            "horizontal_datum": "CGCS2000",
            "georeference": config.georeference.to_dict(),
            "cad_origin_xy": list(config.cad_origin_xy),
            "cad_scale": config.cad_scale,
            "route_offset_xyz_m": list(config.route_offset_xyz_m),
            "height_source": "rel_alt",
            "absolute_height_usage": "diagnostic_only",
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
            "position_source": "srt_cad_locked",
            "position_edit_policy": "uniform_xyz_offset_only",
            "orientation_status": solution.status,
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
            "position_source": "srt_cad_locked",
            "orientation_status": solution.status,
        },
        "points": {
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
        "warnings": list(solution.warnings),
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
        "route_offset_xyz_m": list(config.route_offset_xyz_m),
        "height_source": "rel_alt",
        "abs_alt_usage": "diagnostic_only",
        "visual_pairs": [dict(item) for item in solution.diagnostics],
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
        "# SRT 固定轨迹 + 视觉姿态报告\n\n"
        f"- 轨迹帧数：{len(positions)}\n"
        f"- 可用姿态帧数：{len(solution.rotations)}\n"
        f"- 姿态状态：{solution.status}\n"
        f"- 水平 FOV：{config.horizontal_fov_deg:g}°（用户输入）\n"
        f"- 整条路线统一偏移：{list(config.route_offset_xyz_m)} 米\n"
        "- 位置来源：SRT 经已确认的 CGCS2000 参数投影到 CAD，视觉不得修改。\n"
        "- 高度来源：SRT 相对高度；绝对高度只用于诊断。\n"
        "- 点云：不生成。\n"
        "- 性能原因：跳过位置注册、三角化、BA 和点云维护；仅不写 PLY 并不是主要加速来源。\n"
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
        intrinsics = horizontal_fov_intrinsics(
            metadata[0], metadata[1], config.horizontal_fov_deg
        )
        _write_progress(args.progress_file, *STAGES[0])
        records = load_srt_records(args.srt)
        frame_map = json.loads(args.frame_map.read_text(encoding="utf-8-sig"))
        parse_elapsed = perf_counter() - parse_started
        _write_progress(args.progress_file, *STAGES[1])
        project_started = perf_counter()
        positions = build_fixed_track_positions(records, frame_map, config)
        project_elapsed = perf_counter() - project_started
        _write_progress(args.progress_file, *STAGES[2])

        def visual_progress(value: float, message: str) -> None:
            fraction = 0.22 + max(0.0, min(1.0, float(value))) * 0.33
            _write_progress(
                args.progress_file,
                "extract_visual_constraints",
                message,
                fraction,
            )

        visual_started = perf_counter()
        solution = estimate_video_orientations(
            args.video,
            positions,
            intrinsics,
            config,
            progress=visual_progress,
        )
        visual_elapsed = perf_counter() - visual_started
        _write_progress(args.progress_file, *STAGES[3])
        payloads = _build_payloads(
            dataset=args.dataset,
            run_id=args.run_id,
            positions=positions,
            solution=solution,
            config=config,
            intrinsics=intrinsics,
            video_metadata=metadata,
            phase_timings_seconds={
                "parse_inputs": parse_elapsed,
                "project_srt_track": project_elapsed,
                "estimate_visual_attitude": visual_elapsed,
            },
        )
        run_root.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".srt-fixed-track-", dir=run_root)
        )
        _publish_payloads(staging_root, payloads)
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
