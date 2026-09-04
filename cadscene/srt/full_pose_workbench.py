"""Publish browser workbench artifacts from a complete SRT trajectory."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

from cadscene.core.camera import (
    CameraState,
    decompose_world_from_camera_rotation,
    quaternion_wxyz_to_rotation_matrix,
)
from cadscene.core.coordinates import python_state_to_web_camera


@dataclass(frozen=True)
class FullPoseWorkbenchPayloads:
    camera_track: dict[str, object]
    viewer_scene: dict[str, object]


def build_full_pose_workbench_payloads(
    trajectory: Mapping[str, object],
) -> FullPoseWorkbenchPayloads:
    meta = trajectory.get("meta")
    if not isinstance(meta, Mapping) or meta.get("trajectory_mode") != "srt_full_pose":
        raise ValueError("trajectory must use srt_full_pose mode")
    poses = trajectory.get("poses")
    if not isinstance(poses, list):
        raise ValueError("full-pose trajectory poses must be a list")
    fps = float(trajectory.get("fps", 0.0))
    if fps <= 0.0:
        raise ValueError("full-pose trajectory fps must be positive")
    raw_origin = meta.get("cad_origin_xy")
    if not isinstance(raw_origin, (list, tuple)) or len(raw_origin) != 2:
        raise ValueError("full-pose trajectory cad_origin_xy must contain two values")
    origin_xy = (float(raw_origin[0]), float(raw_origin[1]))
    cad_scale = float(meta.get("cad_scale", 0.0))
    if cad_scale <= 0.0:
        raise ValueError("full-pose trajectory cad_scale must be positive")
    fov = float(meta.get("horizontal_fov_deg", 0.0))
    if not 0.0 < fov < 180.0:
        raise ValueError("full-pose trajectory horizontal_fov_deg is invalid")

    keyframes: list[dict[str, object]] = []
    viewer_route: list[dict[str, object]] = []
    for raw_pose in poses:
        if not isinstance(raw_pose, Mapping) or raw_pose.get("registered") is not True:
            continue
        center = raw_pose.get("center")
        quaternion = raw_pose.get("cam_from_world_quat_wxyz")
        if not isinstance(center, (list, tuple)) or len(center) != 3:
            raise ValueError("registered full-pose center must contain three values")
        cam_from_world = quaternion_wxyz_to_rotation_matrix(quaternion)
        yaw, pitch, roll = decompose_world_from_camera_rotation(cam_from_world.T)
        state = CameraState(
            camera_x=float(center[0]),
            camera_y=float(center[1]),
            camera_z=float(center[2]),
            yaw_deg=float(yaw),
            pitch_deg=float(pitch),
            roll_deg=float(roll),
            fov_deg=fov,
            cad_scale=cad_scale,
        )
        camera = python_state_to_web_camera(state, origin_xy)
        frame = int(raw_pose["frame_index"])
        keyframe = {
            "frame": frame,
            "time": frame / fps,
            "source": "algorithm_prediction",
            "position_source": "srt_cad",
            "orientation_source": "srt_full_pose",
            "camera": camera,
        }
        keyframes.append(keyframe)
        viewer_route.append(
            {
                "frame_index": frame,
                "source_pts": raw_pose.get("source_pts"),
                "source": "srt_pose_prior",
                "position_source": "srt_cad",
                "orientation_available": True,
                "camera": dict(camera),
            }
        )
    if not keyframes:
        raise ValueError("full-pose trajectory contains no registered poses")
    keyframes.sort(key=lambda row: int(row["frame"]))
    viewer_route.sort(key=lambda row: int(row["frame_index"]))

    camera_track = {
        "schema_version": "cadscene_camera_track_pred_v1",
        "fps": fps,
        "keyframes": keyframes,
        "meta": {
            "generated_by": "cadscene.build_srt_full_pose",
            "coordinate_system": "web_cad_world",
            "workflow": "srt_full_pose",
            "pose_prior_schema": "srt_pose_prior_v1",
            "metric_scale_locked": True,
            "position_source": "srt_cad",
            "orientation_source": "srt_full_pose",
            "edit_policy": "six_dof_keyframe_residuals",
            "cad_scale": cad_scale,
        },
    }
    empty_bbox = {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    viewer_scene = {
        "schema_version": "cadscene_sfm_viewer_scene_v1",
        "meta": {
            "generated_by": "cadscene.build_srt_full_pose",
            "coordinate_system": "web_cad_world",
            "point_transform": "none",
            "global_track_transform": "metric_direct_srt_full_pose",
            "anchored_track_transform": "none",
            "pitch_convention": "frontend_pitch_negated_from_python_pitch",
            "workflow": "srt_full_pose",
            "pose_prior_schema": "srt_pose_prior_v1",
            "metric_scale_locked": True,
            "position_source": "srt_cad",
            "orientation_source": "srt_full_pose",
            "edit_policy": "six_dof_keyframe_residuals",
            "point_cloud_generated": False,
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
            "global_sfm_track": viewer_route,
            "anchored_camera_path": [],
        },
        "suggestions": [],
        "quality": {
            "available": False,
            "quality_timeline_ref": "",
            "suggestions_ref": "",
        },
        "warnings": list(meta.get("warnings", [])),
    }
    return FullPoseWorkbenchPayloads(
        camera_track=camera_track,
        viewer_scene=viewer_scene,
    )


def publish_full_pose_workbench_payloads(
    run_root: Path,
    payloads: FullPoseWorkbenchPayloads,
) -> tuple[Path, Path]:
    track = run_root / "03_alignment" / "camera_track_pred.json"
    scene = run_root / "05_viewer_scene" / "sfm_viewer_scene.json"
    _atomic_write_json(track, payloads.camera_track)
    _atomic_write_json(scene, payloads.viewer_scene)
    return track, scene


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
