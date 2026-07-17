"""Robust, scale-preserving SfM to local-ENU Sim3 estimation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .quality import FusionConfig, ReadinessReport, as_points, assess_fusion_readiness


@dataclass(frozen=True)
class Sim3Fit:
    scale: float
    rotation: np.ndarray
    translation: np.ndarray
    inlier_mask: tuple[bool, ...]
    inlier_ratio: float
    mean_error_m: float
    median_error_m: float
    rmse_m: float
    p90_error_m: float
    max_error_m: float
    xy_rmse_m: float
    z_rmse_m: float
    readiness: ReadinessReport

    def transform(self, points: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        array = as_points(points, name="points")
        return self.scale * (array @ self.rotation.T) + self.translation


@dataclass(frozen=True)
class PositionFusionResult:
    metric_positions: np.ndarray
    fused_positions: np.ndarray
    residuals: np.ndarray
    fusion_confidence: np.ndarray
    smoothing_support: np.ndarray


def quat_wxyz_to_matrix(quaternion: Sequence[float]) -> np.ndarray:
    w, x, y, z = (float(value) for value in quaternion)
    norm = float(np.sqrt(w * w + x * x + y * y + z * z))
    if norm <= 1e-12:
        raise ValueError("camera quaternion has zero norm")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def _matrix_to_quat_wxyz(matrix: np.ndarray) -> np.ndarray:
    rotation = np.asarray(matrix, dtype=np.float64)
    if rotation.shape != (3, 3):
        raise ValueError("rotation must have shape (3, 3)")
    trace = float(np.trace(rotation))
    if trace > 0.0:
        root = np.sqrt(trace + 1.0) * 2.0
        quat = np.array([0.25 * root, (rotation[2, 1] - rotation[1, 2]) / root, (rotation[0, 2] - rotation[2, 0]) / root, (rotation[1, 0] - rotation[0, 1]) / root])
    else:
        index = int(np.argmax(np.diag(rotation)))
        if index == 0:
            root = np.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
            quat = np.array([(rotation[2, 1] - rotation[1, 2]) / root, 0.25 * root, (rotation[0, 1] + rotation[1, 0]) / root, (rotation[0, 2] + rotation[2, 0]) / root])
        elif index == 1:
            root = np.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
            quat = np.array([(rotation[0, 2] - rotation[2, 0]) / root, (rotation[0, 1] + rotation[1, 0]) / root, 0.25 * root, (rotation[1, 2] + rotation[2, 1]) / root])
        else:
            root = np.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
            quat = np.array([(rotation[1, 0] - rotation[0, 1]) / root, (rotation[0, 2] + rotation[2, 0]) / root, (rotation[1, 2] + rotation[2, 1]) / root, 0.25 * root])
    return quat / np.linalg.norm(quat)


def rotate_cam_from_world_quat(quaternion_wxyz: Sequence[float], sim3_rotation: np.ndarray) -> list[float]:
    """Transform R_cw after X_new=s R_sim3 X_old+t: R_cw_new=R_cw_old R_sim3^T."""

    rotation = np.asarray(sim3_rotation, dtype=np.float64)
    if rotation.shape != (3, 3) or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6):
        raise ValueError("sim3_rotation must be an orthonormal 3x3 matrix")
    return [float(value) for value in _matrix_to_quat_wxyz(quat_wxyz_to_matrix(quaternion_wxyz) @ rotation.T)]


def fuse_positions(
    metric_positions: Sequence[Sequence[float]] | np.ndarray,
    *,
    frame_times_sec: Sequence[float] | np.ndarray,
    srt_positions: Sequence[Sequence[float]] | np.ndarray,
    srt_valid: Sequence[bool] | np.ndarray,
    smoothing_window_sec: float = 2.0,
    min_smoothing_support: int = 3,
) -> PositionFusionResult:
    """Apply bounded-window median SRT residuals without spanning invalid holes."""

    metric = as_points(metric_positions, name="metric_positions")
    srt = as_points(srt_positions, name="srt_positions")
    times = np.asarray(frame_times_sec, dtype=np.float64)
    valid = np.asarray(srt_valid, dtype=bool)
    if srt.shape != metric.shape or times.shape != (len(metric),) or valid.shape != (len(metric),):
        raise ValueError("metric/SRT positions, frame times and validity must have matching lengths")
    if not np.isfinite(times).all() or smoothing_window_sec <= 0.0 or min_smoothing_support < 1:
        raise ValueError("frame times must be finite; smoothing parameters must be positive")
    residual = srt - metric
    fused = metric.copy()
    confidence = np.full(len(metric), 0.2, dtype=np.float64)
    support = np.zeros(len(metric), dtype=np.int64)
    half_window = float(smoothing_window_sec) / 2.0
    for index in range(len(metric)):
        if not valid[index]:
            continue
        left = index
        while left > 0 and valid[left - 1]:
            left -= 1
        right = index
        while right + 1 < len(metric) and valid[right + 1]:
            right += 1
        window = np.arange(left, right + 1)
        window = window[np.abs(times[window] - times[index]) <= half_window]
        support[index] = len(window)
        if len(window) < min_smoothing_support:
            confidence[index] = 0.45
            continue
        correction = np.median(residual[window], axis=0)
        fused[index] = metric[index] + correction
        confidence[index] = min(0.9, 0.5 + 0.1 * len(window))
    return PositionFusionResult(metric, fused, residual, confidence, support)


def build_fused_trajectory_json(
    raw_trajectory: dict,
    position_result: PositionFusionResult,
    *,
    sim3_rotation: np.ndarray,
    coordinate_meta: dict,
) -> dict:
    """Preserve the legacy trajectory schema while adding fusion provenance."""

    output = deepcopy(raw_trajectory)
    registered = [pose for pose in output.get("poses", []) if pose.get("registered", True)]
    if len(registered) != len(position_result.fused_positions):
        raise ValueError("registered poses do not match fused position count")
    for index, pose in enumerate(registered):
        pose["center"] = [float(value) for value in position_result.fused_positions[index]]
        pose["cam_from_world_quat_wxyz"] = rotate_cam_from_world_quat(pose["cam_from_world_quat_wxyz"], sim3_rotation)
        pose["srt_valid"] = bool(position_result.smoothing_support[index] > 0)
        pose["srt_residual_m"] = float(np.linalg.norm(position_result.residuals[index]))
        pose["fusion_confidence"] = float(position_result.fusion_confidence[index])
        pose["fusion_confidence_components"] = {
            "srt_valid": bool(position_result.smoothing_support[index] > 0),
            "smoothing_support": int(position_result.smoothing_support[index]),
        }
    output["meta"] = {
        **dict(output.get("meta") or {}),
        **coordinate_meta,
        "trajectory_mode": "srt_sfm_fused",
        "position_source": "sfm_shape_plus_srt_low_frequency",
        "orientation_source": "sfm",
        "fusion_confidence_note": "Internal consistency only; not an absolute positioning accuracy probability.",
    }
    return output


def weighted_error_m(residual: np.ndarray, vertical_weight: float) -> np.ndarray:
    """Return sqrt(e_xy² + (vertical_weight * e_z)²) in metres."""

    xy = np.linalg.norm(residual[:, :2], axis=1)
    z = np.abs(residual[:, 2])
    return np.sqrt(xy**2 + (float(vertical_weight) * z) ** 2)


def select_constraint_indices(srt_centers: Sequence[Sequence[float]] | np.ndarray, config: FusionConfig) -> np.ndarray:
    """Spatially deduplicate GPS constraints then retain time-uniform indices."""

    target = as_points(srt_centers, name="srt_centers")
    kept: list[int] = []
    for index, point in enumerate(target):
        if all(float(np.linalg.norm(point[:2] - target[existing, :2])) >= config.spatial_dedupe_m for existing in kept):
            kept.append(index)
    if config.max_constraint_samples < 3:
        raise ValueError("max_constraint_samples must be at least three")
    if len(kept) <= config.max_constraint_samples:
        return np.asarray(kept, dtype=np.int64)
    positions = np.linspace(0, len(kept) - 1, num=config.max_constraint_samples, dtype=np.int64)
    return np.asarray([kept[position] for position in positions], dtype=np.int64)


def _umeyama(source: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    if len(source) < 3:
        raise ValueError("Sim3 requires at least three points")
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean
    variance = float(np.mean(np.sum(source_centered**2, axis=1)))
    if variance <= 1e-12:
        raise ValueError("SfM trajectory is stationary")
    covariance = target_centered.T @ source_centered / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.eye(3)
    if np.linalg.det(u @ vt) < 0.0:
        correction[-1, -1] = -1.0
    rotation = u @ correction @ vt
    scale = float(np.trace(np.diag(singular) @ correction) / variance)
    if not np.isfinite(scale) or scale <= 0.0:
        raise ValueError("Sim3 scale is invalid")
    translation = target_mean - scale * (rotation @ source_mean)
    return scale, rotation, translation


def estimate_sfm_to_srt_sim3(
    sfm_centers: Sequence[Sequence[float]] | np.ndarray,
    srt_centers: Sequence[Sequence[float]] | np.ndarray,
    config: FusionConfig,
) -> Sim3Fit:
    """Estimate SfM→ENU Sim3 using fixed-seed RANSAC and raw-3D Umeyama.

    ``vertical_weight`` is deliberately restricted to RANSAC scoring. Umeyama
    always receives unscaled 3D coordinates.
    """

    source = as_points(sfm_centers, name="sfm_centers")
    target = as_points(srt_centers, name="srt_centers")
    readiness = assess_fusion_readiness(source, target, config)
    if not readiness.accepted:
        reasons = ", ".join(readiness.rejection_reasons)
        raise ValueError(f"partial-SRT fusion is not ready ({reasons}); fall back to sfm_only")
    if config.ransac_iterations < 1 or config.ransac_inlier_threshold_m <= 0.0:
        raise ValueError("RANSAC iteration count and inlier threshold must be positive")
    selected_indices = select_constraint_indices(target, config)
    if len(selected_indices) < 3:
        raise ValueError("too few spatially independent constraints; fall back to sfm_only")

    rng = np.random.default_rng(config.ransac_seed)
    best_mask: np.ndarray | None = None
    best_score: tuple[int, float] | None = None
    for _ in range(config.ransac_iterations):
        candidate = selected_indices[rng.choice(len(selected_indices), size=3, replace=False)]
        try:
            scale, rotation, translation = _umeyama(source[candidate], target[candidate])
        except (ValueError, np.linalg.LinAlgError):
            continue
        residual = target - (scale * (source @ rotation.T) + translation)
        errors = weighted_error_m(residual, config.vertical_weight)
        mask = errors <= config.ransac_inlier_threshold_m
        score = (int(mask.sum()), -float(errors[mask].sum()) if mask.any() else float("-inf"))
        if best_score is None or score > best_score:
            best_mask, best_score = mask, score
    if best_mask is None or int(best_mask.sum()) < 3:
        raise ValueError("Sim3 RANSAC found too few inliers; fall back to sfm_only")

    scale, rotation, translation = _umeyama(source[best_mask], target[best_mask])
    residual = target - (scale * (source @ rotation.T) + translation)
    errors = weighted_error_m(residual, config.vertical_weight)
    inlier_errors = errors[best_mask]
    xy_errors = np.linalg.norm(residual[best_mask, :2], axis=1)
    z_errors = np.abs(residual[best_mask, 2])
    return Sim3Fit(
        scale=scale,
        rotation=rotation,
        translation=translation,
        inlier_mask=tuple(bool(value) for value in best_mask),
        inlier_ratio=float(best_mask.mean()),
        mean_error_m=float(inlier_errors.mean()),
        median_error_m=float(np.median(inlier_errors)),
        rmse_m=float(np.sqrt(np.mean(inlier_errors**2))),
        p90_error_m=float(np.quantile(inlier_errors, 0.9)),
        max_error_m=float(inlier_errors.max()),
        xy_rmse_m=float(np.sqrt(np.mean(xy_errors**2))),
        z_rmse_m=float(np.sqrt(np.mean(z_errors**2))),
        readiness=readiness,
    )
