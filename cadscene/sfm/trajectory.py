from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

import numpy as np


def quat_wxyz_to_matrix(q: Sequence[float]) -> np.ndarray:
    w, x, y, z = [float(v) for v in q]
    n = math.sqrt(w * w + x * x + y * y + z * z)
    if n <= 1e-12:
        return np.eye(3, dtype=np.float64)
    w, x, y, z = w / n, x / n, y / n, z / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def quat_slerp(q0: np.ndarray, q1: np.ndarray, alpha: float) -> np.ndarray:
    a = np.asarray(q0, dtype=np.float64)
    b = np.asarray(q1, dtype=np.float64)
    a = a / max(np.linalg.norm(a), 1e-12)
    b = b / max(np.linalg.norm(b), 1e-12)
    dot = float(np.dot(a, b))
    if dot < 0.0:
        b = -b
        dot = -dot
    if dot > 0.9995:
        out = a + float(alpha) * (b - a)
        return out / max(np.linalg.norm(out), 1e-12)
    theta_0 = math.acos(max(-1.0, min(1.0, dot)))
    theta = theta_0 * float(alpha)
    sin_theta = math.sin(theta)
    sin_theta_0 = math.sin(theta_0)
    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0
    return s0 * a + s1 * b


@dataclass(frozen=True)
class SfmTrajectory:
    frames: np.ndarray
    centers: np.ndarray
    quats_c2w_wxyz: np.ndarray
    fps: float
    width: int
    height: int
    intrinsics: dict
    meta: dict = field(default_factory=dict)

    @property
    def frame_min(self) -> int:
        return int(self.frames[0])

    @property
    def frame_max(self) -> int:
        return int(self.frames[-1])

    def query(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        f = float(frame_index)
        if f <= self.frames[0]:
            return self.centers[0].copy(), quat_wxyz_to_matrix(self.quats_c2w_wxyz[0])
        if f >= self.frames[-1]:
            return self.centers[-1].copy(), quat_wxyz_to_matrix(self.quats_c2w_wxyz[-1])
        j = int(np.searchsorted(self.frames, f, side="right"))
        i = j - 1
        fa, fb = float(self.frames[i]), float(self.frames[j])
        alpha = (f - fa) / max(fb - fa, 1e-9)
        center = self.centers[i] * (1.0 - alpha) + self.centers[j] * alpha
        quat = quat_slerp(self.quats_c2w_wxyz[i], self.quats_c2w_wxyz[j], alpha)
        return center, quat_wxyz_to_matrix(quat)

    def horizontal_fov_deg(self) -> Optional[float]:
        try:
            params = self.intrinsics.get("params", [])
            fx = float(params[0])
            width = float(self.intrinsics.get("width", self.width))
            if fx > 0 and width > 0:
                return float(math.degrees(2.0 * math.atan((width / 2.0) / fx)))
        except Exception:
            return None
        return None


def load_sfm_trajectory(path: str | Path) -> SfmTrajectory:
    with Path(path).open("r", encoding="utf-8-sig") as f:
        data = json.load(f)
    frames: list[int] = []
    centers: list[list[float]] = []
    quats: list[list[float]] = []
    for pose in data.get("poses", []):
        if not pose.get("registered", True):
            continue
        frames.append(int(pose["frame_index"]))
        centers.append([float(v) for v in pose["center"]])
        quats.append([float(v) for v in pose["cam_from_world_quat_wxyz"]])
    if len(frames) < 2:
        raise RuntimeError("SfM 轨迹至少需要 2 个已注册帧。")
    order = np.argsort(frames)
    intrinsics = (data.get("intrinsics") or [{}])[0]
    return SfmTrajectory(
        frames=np.asarray(frames, dtype=np.int64)[order],
        centers=np.asarray(centers, dtype=np.float64)[order],
        quats_c2w_wxyz=np.asarray(quats, dtype=np.float64)[order],
        fps=float(data.get("fps", 25.0)),
        width=int(data.get("width", 0)),
        height=int(data.get("height", 0)),
        intrinsics=intrinsics,
        meta=dict(data.get("meta") or {}),
    )
