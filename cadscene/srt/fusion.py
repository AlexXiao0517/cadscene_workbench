"""Robust, scale-preserving SfM to local-ENU Sim3 estimation."""

from __future__ import annotations

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
