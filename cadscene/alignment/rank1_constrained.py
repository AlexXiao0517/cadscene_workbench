"""Offline Rank-1 SfM/SRT constrained-alignment primitives.

This module is intentionally independent from the standard rank>=2 Sim3
implementation.  It never treats a line trajectory as a fully observable
position-only Sim3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class Rank1Config:
    rank_relative_threshold: float = 0.01
    linearity_ratio_threshold: float = 0.05
    min_baseline: float = 5.0
    min_scale_samples: int = 6
    min_sfm_along_track_delta: float = 1e-6
    along_track_inlier_threshold_m: float = 2.0
    ransac_seed: int = 20_260_721
    ransac_iterations: int = 512
    min_scale: float = 1e-6
    max_scale: float = 1e6
    max_solve_rotation_disagreement_deg: float = 5.0
    max_translation_spread_m: float = 2.0
    smoothing_window_sec: float = 2.0
    min_smoothing_support: int = 3


@dataclass(frozen=True)
class Rank1AxisAnalysis:
    primary_direction: np.ndarray
    singular_values: tuple[float, float, float]
    rank: int
    linearity_ratio: float
    near_linear: bool
    baseline: float
    direction_sign_source: str


@dataclass(frozen=True)
class AlongTrackScaleFit:
    scale: float
    offset: float
    inlier_mask: tuple[bool, ...]
    inlier_count: int
    inlier_ratio: float
    median_along_track_residual_m: float
    rmse_along_track_residual_m: float
    p90_along_track_residual_m: float
    scale_consistency: float


def _points(values: Sequence[Sequence[float]] | np.ndarray, name: str) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64)
    if result.ndim != 2 or result.shape[1] != 3 or len(result) < 2:
        raise ValueError(f"{name} must have shape (N, 3) with N>=2")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} contains NaN or Inf")
    return result


def analyze_rank1_axis(
    points: Sequence[Sequence[float]] | np.ndarray,
    times_sec: Sequence[float] | np.ndarray,
    config: Rank1Config = Rank1Config(),
) -> Rank1AxisAnalysis:
    """Analyze a line trajectory and orient its PCA axis in time-forward order."""

    values = _points(points, "points")
    times = np.asarray(times_sec, dtype=np.float64)
    if times.shape != (len(values),) or not np.isfinite(times).all():
        raise ValueError("times_sec must be finite and match points")
    if np.any(np.diff(times) < 0.0):
        raise ValueError("times_sec must be non-decreasing")
    centered = values - values.mean(axis=0)
    _u, singular, vt = np.linalg.svd(centered, full_matrices=False)
    singular = np.pad(singular, (0, max(0, 3 - len(singular))))[:3]
    leading = float(singular[0])
    threshold = max(1e-9, leading * float(config.rank_relative_threshold))
    rank = int(np.count_nonzero(singular > threshold)) if leading > 0.0 else 0
    linearity = float(singular[1] / leading) if leading > 1e-12 else 0.0
    near_linear = rank == 1 and linearity <= float(config.linearity_ratio_threshold)
    direction = np.asarray(vt[0], dtype=np.float64)
    temporal_delta = values[-1] - values[0]
    if float(np.dot(direction, temporal_delta)) < 0.0:
        direction = -direction
    direction /= max(float(np.linalg.norm(direction)), 1e-12)
    along = centered @ direction
    baseline = float(np.ptp(along))
    if baseline < float(config.min_baseline):
        raise ValueError(f"Rank-1 baseline is too short ({baseline:.6g})")
    if not near_linear:
        raise ValueError(f"Rank-1 solver requires rank=1 near-linear input (rank={rank}, linearity_ratio={linearity:.6g})")
    return Rank1AxisAnalysis(
        primary_direction=direction,
        singular_values=tuple(float(value) for value in singular),
        rank=rank,
        linearity_ratio=linearity,
        near_linear=True,
        baseline=baseline,
        direction_sign_source="time_forward_endpoint_delta",
    )


def project_along_track(
    points: Sequence[Sequence[float]] | np.ndarray,
    direction: Sequence[float] | np.ndarray,
    reference: Sequence[float] | np.ndarray,
) -> np.ndarray:
    values = _points(points, "points")
    axis = np.asarray(direction, dtype=np.float64)
    origin = np.asarray(reference, dtype=np.float64)
    if axis.shape != (3,) or origin.shape != (3,) or not np.isfinite(axis).all() or not np.isfinite(origin).all():
        raise ValueError("direction and reference must be finite 3-vectors")
    norm = float(np.linalg.norm(axis))
    if norm <= 1e-12:
        raise ValueError("direction must be non-zero")
    return (values - origin) @ (axis / norm)


def estimate_along_track_scale(
    u_sfm: Sequence[float] | np.ndarray,
    u_srt: Sequence[float] | np.ndarray,
    config: Rank1Config = Rank1Config(),
) -> AlongTrackScaleFit:
    """Fit ``u_srt = scale*u_sfm + offset`` using deterministic 1D RANSAC."""

    source = np.asarray(u_sfm, dtype=np.float64)
    target = np.asarray(u_srt, dtype=np.float64)
    if source.ndim != 1 or target.shape != source.shape or len(source) < config.min_scale_samples:
        raise ValueError("along-track scale requires matching 1D samples")
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError("along-track samples contain NaN or Inf")
    if config.ransac_iterations < 1 or config.along_track_inlier_threshold_m <= 0.0:
        raise ValueError("Rank-1 RANSAC settings must be positive")

    rng = np.random.default_rng(config.ransac_seed)
    best_mask: np.ndarray | None = None
    best_model: tuple[float, float] | None = None
    best_score: tuple[int, float] | None = None
    for _ in range(config.ransac_iterations):
        i, j = (int(value) for value in rng.choice(len(source), size=2, replace=False))
        delta = float(source[j] - source[i])
        if abs(delta) <= config.min_sfm_along_track_delta:
            continue
        scale = float((target[j] - target[i]) / delta)
        if not config.min_scale <= scale <= config.max_scale:
            continue
        offset = float(np.median(target - scale * source))
        residual = target - (scale * source + offset)
        mask = np.abs(residual) <= config.along_track_inlier_threshold_m
        score = (int(mask.sum()), -float(np.median(np.abs(residual[mask]))) if mask.any() else float("-inf"))
        if best_score is None or score > best_score:
            best_mask, best_model, best_score = mask, (scale, offset), score
    if best_mask is None or best_model is None or int(best_mask.sum()) < config.min_scale_samples:
        raise ValueError("Rank-1 along-track scale RANSAC found too few inliers")

    inlier_indices = np.flatnonzero(best_mask)
    pair_slopes: list[float] = []
    for pos, i in enumerate(inlier_indices):
        for j in inlier_indices[pos + 1 :]:
            delta = float(source[j] - source[i])
            if abs(delta) > config.min_sfm_along_track_delta:
                pair_slopes.append(float((target[j] - target[i]) / delta))
    scale = float(np.median(pair_slopes)) if pair_slopes else best_model[0]
    if not config.min_scale <= scale <= config.max_scale:
        raise ValueError("Rank-1 along-track scale is non-positive or out of range")
    offset = float(np.median(target[best_mask] - scale * source[best_mask]))
    residual = target - (scale * source + offset)
    final_mask = np.abs(residual) <= config.along_track_inlier_threshold_m
    if int(final_mask.sum()) < config.min_scale_samples:
        raise ValueError("Rank-1 along-track scale refinement found too few inliers")
    abs_residual = np.abs(residual[final_mask])
    slope_array = np.asarray(pair_slopes, dtype=np.float64)
    consistency = float(np.median(np.abs(slope_array - scale)) / max(abs(scale), 1e-12)) if len(slope_array) else 0.0
    return AlongTrackScaleFit(
        scale=scale,
        offset=offset,
        inlier_mask=tuple(bool(value) for value in final_mask),
        inlier_count=int(final_mask.sum()),
        inlier_ratio=float(final_mask.mean()),
        median_along_track_residual_m=float(np.median(abs_residual)),
        rmse_along_track_residual_m=float(np.sqrt(np.mean(abs_residual**2))),
        p90_along_track_residual_m=float(np.quantile(abs_residual, 0.9)),
        scale_consistency=consistency,
    )
