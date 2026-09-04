"""Six-DoF residual fitting on top of an immutable camera-pose prior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


@dataclass(frozen=True)
class PosePrior:
    frame_index: int
    center: np.ndarray
    world_from_camera: np.ndarray


@dataclass(frozen=True)
class ManualPose:
    frame_index: int
    center: np.ndarray
    world_from_camera: np.ndarray


@dataclass(frozen=True)
class PosePriorResidualFit:
    base_frames: np.ndarray
    base_centers: np.ndarray
    base_world_from_camera: np.ndarray
    key_frames: np.ndarray
    position_residuals: np.ndarray
    rotation_residuals: np.ndarray

    def base_pose_at(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        return _interpolate_pose(
            self.base_frames,
            self.base_centers,
            self.base_world_from_camera,
            frame_index,
            allow_outside=False,
        )

    def residual_at(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        if len(self.key_frames) == 0:
            return np.zeros(3, dtype=np.float64), np.eye(3, dtype=np.float64)
        return _interpolate_pose(
            self.key_frames,
            self.position_residuals,
            self.rotation_residuals,
            frame_index,
            allow_outside=True,
        )

    def pose_at(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        base_center, base_rotation = self.base_pose_at(frame_index)
        delta_center, delta_rotation = self.residual_at(frame_index)
        return base_center + delta_center, delta_rotation @ base_rotation


def fit_pose_prior_residuals(
    base_poses: Sequence[PosePrior],
    manual_poses: Sequence[ManualPose],
) -> PosePriorResidualFit:
    base = _validated_poses(base_poses, label="base")
    if not base:
        raise ValueError("at least one base pose is required")
    manual = _validated_poses(manual_poses, label="manual")
    base_frames = np.asarray([pose.frame_index for pose in base], dtype=np.int64)
    base_centers = np.asarray([pose.center for pose in base], dtype=np.float64)
    base_rotations = np.asarray(
        [pose.world_from_camera for pose in base], dtype=np.float64
    )
    key_frames: list[int] = []
    position_residuals: list[np.ndarray] = []
    rotation_residuals: list[np.ndarray] = []
    for pose in manual:
        try:
            base_center, base_rotation = _interpolate_pose(
                base_frames,
                base_centers,
                base_rotations,
                pose.frame_index,
                allow_outside=False,
            )
        except ValueError as exc:
            raise ValueError(
                f"manual frame {pose.frame_index} has no base pose"
            ) from exc
        key_frames.append(pose.frame_index)
        position_residuals.append(pose.center - base_center)
        rotation_residuals.append(pose.world_from_camera @ base_rotation.T)
    return PosePriorResidualFit(
        base_frames=base_frames,
        base_centers=base_centers,
        base_world_from_camera=base_rotations,
        key_frames=np.asarray(key_frames, dtype=np.int64),
        position_residuals=(
            np.asarray(position_residuals, dtype=np.float64).reshape(-1, 3)
            if position_residuals
            else np.empty((0, 3), dtype=np.float64)
        ),
        rotation_residuals=(
            np.asarray(rotation_residuals, dtype=np.float64).reshape(-1, 3, 3)
            if rotation_residuals
            else np.empty((0, 3, 3), dtype=np.float64)
        ),
    )


def _validated_poses(
    poses: Sequence[PosePrior] | Sequence[ManualPose], *, label: str
) -> list[PosePrior] | list[ManualPose]:
    source = sorted(poses, key=lambda pose: int(pose.frame_index))
    ordered: list[PosePrior] | list[ManualPose] = []
    frames: set[int] = set()
    for pose in source:
        frame = int(pose.frame_index)
        if frame in frames:
            raise ValueError(f"duplicate {label} pose frame: {frame}")
        frames.add(frame)
        center = np.asarray(pose.center, dtype=np.float64)
        matrix = np.asarray(pose.world_from_camera, dtype=np.float64)
        if center.shape != (3,) or matrix.shape != (3, 3):
            raise ValueError(f"{label} pose must contain a 3-vector and 3x3 rotation")
        if not np.all(np.isfinite(center)) or not np.all(np.isfinite(matrix)):
            raise ValueError(f"{label} pose values must be finite")
        try:
            normalized = Rotation.from_matrix(matrix).as_matrix()
        except ValueError as exc:
            raise ValueError(f"{label} pose rotation is invalid") from exc
        ordered.append(
            type(pose)(
                frame_index=frame,
                center=center.copy(),
                world_from_camera=normalized,
            )
        )
    return ordered


def _interpolate_pose(
    frames: np.ndarray,
    positions: np.ndarray,
    rotations: np.ndarray,
    frame_index: float,
    *,
    allow_outside: bool,
) -> tuple[np.ndarray, np.ndarray]:
    frame = float(frame_index)
    if not np.isfinite(frame):
        raise ValueError("frame index must be finite")
    if len(frames) == 0:
        raise ValueError("pose sequence is empty")
    if frame <= float(frames[0]):
        if not allow_outside and frame < float(frames[0]):
            raise ValueError("frame is before pose range")
        return positions[0].copy(), rotations[0].copy()
    if frame >= float(frames[-1]):
        if not allow_outside and frame > float(frames[-1]):
            raise ValueError("frame is after pose range")
        return positions[-1].copy(), rotations[-1].copy()
    second = int(np.searchsorted(frames, frame, side="right"))
    first = second - 1
    first_frame = float(frames[first])
    second_frame = float(frames[second])
    alpha = (frame - first_frame) / max(second_frame - first_frame, 1e-12)
    position = positions[first] * (1.0 - alpha) + positions[second] * alpha
    rotation = Slerp(
        [first_frame, second_frame],
        Rotation.from_matrix(np.asarray([rotations[first], rotations[second]])),
    )([frame]).as_matrix()[0]
    return position, rotation
