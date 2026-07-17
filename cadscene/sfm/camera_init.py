from __future__ import annotations

import math
from pathlib import Path

from cadscene.core.camera import decompose_world_from_camera_rotation
from cadscene.core.io import read_json
from cadscene.sfm.trajectory import quat_wxyz_to_matrix


def load_sfm_camera_initialization(path: str | Path) -> dict:
    """读取首个注册 SfM 帧，仅导出前端相机姿态与 FOV，不导出位置。"""

    payload = read_json(path)
    poses = sorted(
        (pose for pose in payload.get("poses", []) if pose.get("registered", True)),
        key=lambda pose: int(pose.get("frame_index", 0)),
    )
    if not poses:
        raise RuntimeError("SfM trajectory has no registered camera pose")
    pose = poses[0]
    cam_from_world = quat_wxyz_to_matrix(pose["cam_from_world_quat_wxyz"])
    world_from_camera = cam_from_world.T
    yaw, python_pitch, roll = decompose_world_from_camera_rotation(world_from_camera)

    intrinsics = (payload.get("intrinsics") or [{}])[0]
    params = intrinsics.get("params") or []
    width = float(intrinsics.get("width") or payload.get("width") or 0)
    fx = float(params[0]) if params else 0.0
    fov = math.degrees(2.0 * math.atan((width / 2.0) / fx)) if width > 0 and fx > 0 else 70.0
    return {
        "frame_index": int(pose.get("frame_index", 0)),
        "yaw": float(yaw),
        "pitch": float(-python_pitch),
        "roll": float(roll),
        "fov": float(fov),
        "generated_by": "cadscene.sfm.camera_init",
        "position_preserved": True,
        "orientation_safe_to_apply": False,
        "safe_fields": ["fov"],
        "warning": "SfM world 尚未与 CAD/重力方向对齐，raw yaw/pitch/roll 仅供诊断。",
    }
