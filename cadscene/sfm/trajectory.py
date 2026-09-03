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
    unregistered_frames: np.ndarray = field(
        default_factory=lambda: np.asarray([], dtype=np.int64)
    )
    position_frames: np.ndarray = field(
        default_factory=lambda: np.asarray([], dtype=np.int64)
    )
    position_centers: np.ndarray = field(
        default_factory=lambda: np.empty((0, 3), dtype=np.float64)
    )

    def __post_init__(self) -> None:
        if len(self.position_frames) == 0 and len(self.frames):
            object.__setattr__(self, "position_frames", self.frames.copy())
            object.__setattr__(self, "position_centers", self.centers.copy())
        if len(self.position_frames) != len(self.position_centers):
            raise ValueError("position frames and centers must have matching lengths")

    @property
    def frame_min(self) -> int:
        if len(self.position_frames):
            return int(self.position_frames[0])
        if len(self.unregistered_frames):
            return min(int(self.frames[0]), int(self.unregistered_frames[0]))
        return int(self.frames[0])

    @property
    def frame_max(self) -> int:
        if len(self.position_frames):
            return int(self.position_frames[-1])
        if len(self.unregistered_frames):
            return max(int(self.frames[-1]), int(self.unregistered_frames[-1]))
        return int(self.frames[-1])

    def is_frame_registered(self, frame_index: float) -> bool:
        if not len(self.frames):
            return False
        if not len(self.unregistered_frames):
            return True
        frame = float(frame_index)
        index = int(np.searchsorted(self.unregistered_frames, frame, side="left"))
        return not (
            index < len(self.unregistered_frames)
            and math.isclose(
                float(self.unregistered_frames[index]),
                frame,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )

    def center_at(self, frame_index: float) -> np.ndarray:
        if not len(self.position_frames):
            raise ValueError("trajectory has no available camera positions")
        frame = float(frame_index)
        if frame <= self.position_frames[0]:
            return self.position_centers[0].copy()
        if frame >= self.position_frames[-1]:
            return self.position_centers[-1].copy()
        second = int(np.searchsorted(self.position_frames, frame, side="right"))
        first = second - 1
        first_frame = float(self.position_frames[first])
        second_frame = float(self.position_frames[second])
        alpha = (frame - first_frame) / max(second_frame - first_frame, 1e-9)
        return (
            self.position_centers[first] * (1.0 - alpha)
            + self.position_centers[second] * alpha
        )

    def is_orientation_available(self, frame_index: float) -> bool:
        return self.is_frame_registered(frame_index)

    def orientation_at(self, frame_index: float) -> np.ndarray:
        if not self.is_orientation_available(frame_index):
            raise ValueError(
                f"trajectory orientation is unavailable at frame {float(frame_index):g}"
            )
        _center, rotation = self.query(frame_index)
        return rotation

    def query(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        f = float(frame_index)
        if not len(self.frames):
            raise ValueError(f"trajectory orientation is unavailable at frame {f:g}")
        if not self.is_frame_registered(f):
            raise ValueError(
                f"trajectory frame {f:g} is explicitly unregistered"
            )
        if f <= self.frames[0]:
            return self.center_at(f), quat_wxyz_to_matrix(self.quats_c2w_wxyz[0])
        if f >= self.frames[-1]:
            return self.center_at(f), quat_wxyz_to_matrix(self.quats_c2w_wxyz[-1])
        j = int(np.searchsorted(self.frames, f, side="right"))
        i = j - 1
        fa, fb = float(self.frames[i]), float(self.frames[j])
        if len(self.unregistered_frames):
            gap_start = int(
                np.searchsorted(self.unregistered_frames, fa, side="right")
            )
            gap_end = int(
                np.searchsorted(self.unregistered_frames, fb, side="left")
            )
            if gap_start < gap_end:
                raise ValueError(
                    f"trajectory query at frame {f:g} crosses an unregistered gap"
                )
        alpha = (f - fa) / max(fb - fa, 1e-9)
        center = self.center_at(f)
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
    unregistered_frames: list[int] = []
    position_frames: list[int] = []
    position_centers: list[list[float]] = []
    meta = dict(data.get("meta") or {})
    fixed_track = meta.get("position_source") == "srt_cad_locked"
    for pose in data.get("poses", []):
        frame_index = int(pose["frame_index"])
        position_available = bool(
            pose.get("position_available", pose.get("registered", True))
        )
        orientation_available = bool(
            pose.get("orientation_available", pose.get("registered", True))
        )
        if position_available:
            position_frames.append(frame_index)
            position_centers.append([float(v) for v in pose["center"]])
        if not orientation_available:
            unregistered_frames.append(frame_index)
            continue
        frames.append(frame_index)
        centers.append([float(v) for v in pose["center"]])
        quats.append([float(v) for v in pose["cam_from_world_quat_wxyz"]])
    if fixed_track and len(position_frames) < 2:
        raise RuntimeError("fixed SRT track requires at least two camera positions")
    if not fixed_track and len(frames) < 2:
        raise RuntimeError("SfM 轨迹至少需要 2 个已注册帧。")
    order = np.argsort(frames)
    position_order = np.argsort(position_frames)
    intrinsics = (data.get("intrinsics") or [{}])[0]
    return SfmTrajectory(
        frames=np.asarray(frames, dtype=np.int64)[order],
        centers=np.asarray(centers, dtype=np.float64).reshape(-1, 3)[order],
        quats_c2w_wxyz=np.asarray(quats, dtype=np.float64).reshape(-1, 4)[order],
        fps=float(data.get("fps", 25.0)),
        width=int(data.get("width", 0)),
        height=int(data.get("height", 0)),
        intrinsics=intrinsics,
        meta=meta,
        unregistered_frames=np.asarray(
            sorted(set(unregistered_frames)), dtype=np.int64
        ),
        position_frames=np.asarray(position_frames, dtype=np.int64)[position_order],
        position_centers=np.asarray(
            position_centers, dtype=np.float64
        ).reshape(-1, 3)[position_order],
    )
