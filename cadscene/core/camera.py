from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CameraState:
    """Python 后端使用的相机状态，坐标单位为 CAD meters。"""

    camera_x: float = 0.0
    camera_y: float = 0.0
    camera_z: float = 0.0
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    fov_deg: float = 70.0
    cad_scale: float = 1.0

    def to_row(self, frame_index: int | None = None, status: str = "ok") -> dict:
        row = {
            "camera_x": float(self.camera_x),
            "camera_y": float(self.camera_y),
            "camera_z": float(self.camera_z),
            "yaw": float(self.yaw_deg),
            "pitch": float(self.pitch_deg),
            "roll": float(self.roll_deg),
            "fov": float(self.fov_deg),
            "status": status,
        }
        if frame_index is not None:
            row["frame_index"] = int(frame_index)
        return row

    @classmethod
    def from_row(cls, row: dict, cad_scale: float = 1.0) -> "CameraState":
        return cls(
            camera_x=float(row.get("camera_x", row.get("x", 0.0))),
            camera_y=float(row.get("camera_y", row.get("y", 0.0))),
            camera_z=float(row.get("camera_z", row.get("z", 0.0))),
            yaw_deg=float(row.get("yaw", row.get("yaw_deg", 0.0))),
            pitch_deg=float(row.get("pitch", row.get("pitch_deg", 0.0))),
            roll_deg=float(row.get("roll", row.get("roll_deg", 0.0))),
            fov_deg=float(row.get("fov", row.get("fov_deg", 70.0))),
            cad_scale=float(row.get("cad_scale", cad_scale)),
        )


def decompose_world_from_camera_rotation(rotation: np.ndarray) -> tuple[float, float, float]:
    """将 world-from-camera 旋转矩阵分解为后端 yaw/pitch/roll。"""

    matrix = np.asarray(rotation, dtype=np.float64)
    right = matrix[:, 0]
    forward = matrix[:, 2]
    fx, fy, fz = float(forward[0]), float(forward[1]), float(forward[2])
    pitch = math.asin(max(-1.0, min(1.0, -fz)))
    yaw = math.atan2(fx, fy)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    forward0 = np.asarray([sy * cp, cy * cp, -sp], dtype=np.float64)
    right0 = np.asarray([cy, -sy, 0.0], dtype=np.float64)
    right0 /= max(np.linalg.norm(right0), 1e-12)
    down0 = np.cross(forward0, right0)
    down0 /= max(np.linalg.norm(down0), 1e-12)
    roll = math.atan2(float(np.dot(right, down0)), float(np.dot(right, right0)))
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)
