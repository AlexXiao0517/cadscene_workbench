from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from cadscene.core.camera import CameraState


@dataclass(frozen=True)
class ProjectedPoint:
    u: float
    v: float
    depth_m: float
    distance_m: float

    def __getitem__(self, index: int) -> float:
        return (self.u, self.v, self.depth_m, self.distance_m)[index]


def world_from_camera_rotation(camera: CameraState) -> np.ndarray:
    """根据后端 CameraState 生成 camera-to-CAD meters 旋转矩阵。"""

    yaw = math.radians(camera.yaw_deg)
    pitch = math.radians(max(-89.5, min(89.5, camera.pitch_deg)))
    roll = math.radians(camera.roll_deg)
    forward = np.asarray(
        [math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), -math.sin(pitch)],
        dtype=np.float64,
    )
    forward /= max(np.linalg.norm(forward), 1e-12)
    right = np.asarray([math.cos(yaw), -math.sin(yaw), 0.0], dtype=np.float64)
    right /= max(np.linalg.norm(right), 1e-12)
    down = np.cross(forward, right)
    down /= max(np.linalg.norm(down), 1e-12)
    if abs(roll) > 1e-12:
        c, s = math.cos(roll), math.sin(roll)
        right0, down0 = right.copy(), down.copy()
        right = c * right0 + s * down0
        down = -s * right0 + c * down0
    return np.column_stack([right, down, forward])


def _point3(point: Sequence[float]) -> np.ndarray:
    arr = np.asarray(point, dtype=np.float64)
    if arr.shape[0] == 2:
        return np.asarray([arr[0], arr[1], 0.0], dtype=np.float64)
    return arr[:3].astype(np.float64)


def project_point(
    point_cad_m: Sequence[float],
    camera: CameraState,
    *,
    width: int,
    height: int,
    near_plane_m: float = 0.05,
) -> ProjectedPoint | None:
    """将 CAD meters 点投影到图像平面；输入 pitch 为 Python 后端约定。"""

    point = _point3(point_cad_m)
    center = np.asarray([camera.camera_x, camera.camera_y, camera.camera_z], dtype=np.float64)
    rotation = world_from_camera_rotation(camera)
    cam = rotation.T @ (point - center)
    depth = float(cam[2])
    if depth <= near_plane_m:
        return None
    f = float(width) / (2.0 * math.tan(math.radians(camera.fov_deg) / 2.0))
    u = float(width) * 0.5 + float(cam[0]) * f / depth
    v = float(height) * 0.5 + float(cam[1]) * f / depth
    return ProjectedPoint(u=u, v=v, depth_m=depth, distance_m=float(np.linalg.norm(point - center)))


def style_alpha_for_distance(
    distance_m: float,
    *,
    base_alpha: float,
    faded_overlay: bool,
    fade_start_m: float,
    max_distance_m: float,
) -> float:
    if distance_m > max_distance_m:
        return 0.0
    alpha = max(0.0, min(1.0, float(base_alpha)))
    if not faded_overlay or max_distance_m <= fade_start_m:
        return alpha
    if distance_m <= fade_start_m:
        return alpha
    t = (float(distance_m) - float(fade_start_m)) / max(float(max_distance_m) - float(fade_start_m), 1e-9)
    return alpha * max(0.0, 1.0 - t)


def project_polyline(
    points_cad_m: np.ndarray,
    camera: CameraState,
    *,
    width: int,
    height: int,
    max_distance_m: float,
    near_plane_m: float = 0.05,
) -> list[ProjectedPoint | None]:
    out: list[ProjectedPoint | None] = []
    for point in np.asarray(points_cad_m, dtype=np.float64):
        projected = project_point(point, camera, width=width, height=height, near_plane_m=near_plane_m)
        if projected is not None and projected.distance_m > max_distance_m:
            projected = None
        out.append(projected)
    return out
