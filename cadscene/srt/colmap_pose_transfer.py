"""Transfer COLMAP orientations onto an authoritative SRT camera route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from cadscene.core.sim3 import Sim3
from cadscene.sfm.trajectory import SfmTrajectory
from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackPosition,
    OrientationSolution,
)


class ColmapPoseTransferError(RuntimeError):
    """Raised when a sparse reconstruction cannot be registered to SRT."""


@dataclass(frozen=True)
class ColmapPoseTransfer:
    centers: dict[int, np.ndarray]
    world_from_camera: dict[int, np.ndarray]
    sim3: Sim3
    registered_frames: tuple[int, ...]
    unregistered_frames: tuple[int, ...]
    inlier_frames: tuple[int, ...]
    alignment_residuals_m: dict[int, float]


def transfer_colmap_pose_to_srt(
    reconstruction: SfmTrajectory,
    positions: Sequence[FixedTrackPosition],
    *,
    max_alignment_residual_m: float = 5.0,
    ransac_iterations: int = 256,
) -> ColmapPoseTransfer:
    if not np.isfinite(max_alignment_residual_m) or max_alignment_residual_m <= 0.0:
        raise ValueError("max_alignment_residual_m must be positive and finite")
    by_frame = {int(item.frame_index): item for item in positions}
    paired_frames = [
        int(frame) for frame in reconstruction.frames if int(frame) in by_frame
    ]
    if len(paired_frames) < 3:
        raise ColmapPoseTransferError("COLMAP 与 SRT 同帧配准至少需要三个相机中心")
    source = np.asarray(
        [reconstruction.center_at(frame) for frame in paired_frames],
        dtype=np.float64,
    )
    target = np.asarray(
        [by_frame[frame].center for frame in paired_frames],
        dtype=np.float64,
    )
    if _geometry_rank(source) < 2 or _geometry_rank(target) < 2:
        raise ColmapPoseTransferError("COLMAP 与 SRT 相机中心几何退化，无法确定三维朝向")
    sim3, inlier_mask = _robust_sim3(
        source,
        target,
        threshold=max_alignment_residual_m,
        iterations=ransac_iterations,
    )
    transformed = sim3.apply(source)
    errors = np.linalg.norm(transformed - target, axis=1)
    centers = {
        int(item.frame_index): np.asarray(item.center, dtype=np.float64).copy()
        for item in positions
    }
    world_from_camera = {
        frame: sim3.rotation @ reconstruction.orientation_at(frame).T
        for frame in paired_frames
        if reconstruction.is_orientation_available(frame)
    }
    registered = tuple(sorted(world_from_camera))
    return ColmapPoseTransfer(
        centers=centers,
        world_from_camera=world_from_camera,
        sim3=sim3,
        registered_frames=registered,
        unregistered_frames=tuple(sorted(set(centers) - set(registered))),
        inlier_frames=tuple(
            frame for frame, keep in zip(paired_frames, inlier_mask) if bool(keep)
        ),
        alignment_residuals_m={
            frame: float(error) for frame, error in zip(paired_frames, errors)
        },
    )


def orientation_solution_from_colmap_transfer(
    transfer: ColmapPoseTransfer,
    positions: Sequence[FixedTrackPosition],
    *,
    max_interpolation_gap_sec: float,
) -> OrientationSolution:
    if max_interpolation_gap_sec <= 0.0 or not np.isfinite(max_interpolation_gap_sec):
        raise ValueError("max_interpolation_gap_sec must be positive and finite")
    ordered = sorted(positions, key=lambda item: int(item.frame_index))
    by_frame = {int(item.frame_index): item for item in ordered}
    registered = [frame for frame in transfer.registered_frames if frame in by_frame]
    rotations: dict[int, np.ndarray] = {
        frame: transfer.world_from_camera[frame].T for frame in registered
    }
    components: dict[int, int] = {}
    component = 0
    for index, frame in enumerate(registered):
        if index:
            previous = registered[index - 1]
            elapsed = by_frame[frame].pts_time_sec - by_frame[previous].pts_time_sec
            if elapsed > max_interpolation_gap_sec:
                component += 1
        components[frame] = component
    for item in ordered:
        frame = int(item.frame_index)
        if frame in rotations or not registered:
            continue
        second_index = int(np.searchsorted(registered, frame, side="right"))
        if second_index == 0 or second_index >= len(registered):
            continue
        first = registered[second_index - 1]
        second = registered[second_index]
        if components[first] != components[second]:
            continue
        first_time = float(by_frame[first].pts_time_sec)
        second_time = float(by_frame[second].pts_time_sec)
        current_time = float(item.pts_time_sec)
        if second_time - first_time > max_interpolation_gap_sec:
            continue
        world_from_camera = Slerp(
            [first_time, second_time],
            Rotation.from_matrix(
                [transfer.world_from_camera[first], transfer.world_from_camera[second]]
            ),
        )([current_time]).as_matrix()[0]
        rotations[frame] = world_from_camera.T
        components[frame] = components[first]
    count = len(rotations)
    status = "ready" if count == len(ordered) and count else "partial" if count else "unavailable"
    warnings = (
        ()
        if status == "ready"
        else (f"COLMAP 姿态仅覆盖 {count}/{len(ordered)} 个 SRT 轨迹帧",)
    )
    residuals = list(transfer.alignment_residuals_m.values())
    diagnostics = (
        {
            "solver": "colmap_sparse_reconstruction",
            "registered_frame_count": len(transfer.registered_frames),
            "inlier_frame_count": len(transfer.inlier_frames),
            "alignment_residual_m_mean": (
                float(np.mean(residuals)) if residuals else None
            ),
            "alignment_residual_m_max": (
                float(np.max(residuals)) if residuals else None
            ),
            "sim3": transfer.sim3.to_dict(),
        },
    )
    recommended = registered[len(registered) // 2] if registered else None
    return OrientationSolution(
        status=status,
        rotations=rotations,
        relative_rotations=dict(rotations),
        component_ids=components,
        recommended_anchor_frame=recommended,
        diagnostics=diagnostics,
        warnings=warnings,
    )


def _geometry_rank(points: np.ndarray) -> int:
    centered = np.asarray(points, dtype=np.float64) - np.mean(points, axis=0)
    return int(np.linalg.matrix_rank(centered, tol=1e-8))


def _umeyama(source: np.ndarray, target: np.ndarray) -> Sim3:
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    variance = float(np.mean(np.sum(source_centered * source_centered, axis=1)))
    if variance <= 1e-12 or _geometry_rank(source) < 2 or _geometry_rank(target) < 2:
        raise ColmapPoseTransferError("COLMAP/SRT 配准样本几何退化")
    covariance = target_centered.T @ source_centered / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.eye(3, dtype=np.float64)
    if np.linalg.det(u @ vt) < 0.0:
        correction[-1, -1] = -1.0
    rotation = u @ correction @ vt
    scale = float(np.sum(singular * np.diag(correction)) / variance)
    if not np.isfinite(scale) or scale <= 0.0:
        raise ColmapPoseTransferError("COLMAP/SRT 配准比例无效")
    translation = target_mean - scale * (rotation @ source_mean)
    return Sim3(scale=scale, rotation=rotation, translation=translation)


def _robust_sim3(
    source: np.ndarray,
    target: np.ndarray,
    *,
    threshold: float,
    iterations: int,
) -> tuple[Sim3, np.ndarray]:
    count = len(source)
    rng = np.random.default_rng(0)
    best_mask: np.ndarray | None = None
    best_score: tuple[int, float] | None = None
    attempts = 1 if count == 3 else max(1, int(iterations))
    for _ in range(attempts):
        indices = np.arange(count) if count == 3 else rng.choice(count, 3, replace=False)
        try:
            candidate = _umeyama(source[indices], target[indices])
        except (ColmapPoseTransferError, np.linalg.LinAlgError):
            continue
        errors = np.linalg.norm(candidate.apply(source) - target, axis=1)
        mask = errors <= threshold
        score = (
            int(mask.sum()),
            -float(np.median(errors[mask])) if mask.any() else float("-inf"),
        )
        if best_score is None or score > best_score:
            best_score = score
            best_mask = mask
    minimum = max(3, int(np.ceil(count * 0.5)))
    if best_mask is None or int(best_mask.sum()) < minimum:
        raise ColmapPoseTransferError("COLMAP/SRT 鲁棒配准内点不足")
    fitted = _umeyama(source[best_mask], target[best_mask])
    errors = np.linalg.norm(fitted.apply(source) - target, axis=1)
    final_mask = errors <= threshold
    if int(final_mask.sum()) < minimum:
        raise ColmapPoseTransferError("COLMAP/SRT 配准残差超过门槛")
    return _umeyama(source[final_mask], target[final_mask]), final_mask
