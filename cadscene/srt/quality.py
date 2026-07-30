"""Quality gates for partial-SRT fusion inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class FusionConfig:
    min_common_frames: int = 12
    min_baseline_m: float = 5.0
    min_independent_positions: int = 3
    vertical_weight: float = 0.5
    ransac_seed: int = 20_260_717
    ransac_iterations: int = 256
    ransac_inlier_threshold_m: float = 3.0
    spatial_dedupe_m: float = 0.75
    max_constraint_samples: int = 200
    rank_relative_threshold: float = 0.01
    linear_ratio_threshold: float = 0.05


@dataclass(frozen=True)
class ReadinessReport:
    accepted: bool
    common_frame_count: int
    distinct_position_count: int
    xy_baseline_m: float
    singular_values: tuple[float, float, float]
    spatial_rank: int
    vertical_observability: str
    rejection_reasons: tuple[str, ...]


def as_points(points: Sequence[Sequence[float]] | np.ndarray, *, name: str) -> np.ndarray:
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 3:
        raise ValueError(f"{name} must have shape (N, 3)")
    if not np.isfinite(array).all():
        raise ValueError(f"{name} contains NaN or Inf")
    return array


def _xy_baseline(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    # Bounding-box diagonal is an O(N) conservative spatial baseline.
    extent = np.ptp(points[:, :2], axis=0)
    return float(np.linalg.norm(extent))


def _distinct_positions(points: np.ndarray, tolerance_m: float) -> int:
    if not len(points):
        return 0
    kept: list[np.ndarray] = []
    for point in points:
        if all(float(np.linalg.norm(point[:2] - existing[:2])) >= tolerance_m for existing in kept):
            kept.append(point)
    return len(kept)


def assess_fusion_readiness(
    sfm_centers: Sequence[Sequence[float]] | np.ndarray,
    srt_centers: Sequence[Sequence[float]] | np.ndarray,
    config: FusionConfig,
) -> ReadinessReport:
    """Reject insufficient, stationary and near-linear constraints before Sim3."""

    sfm = as_points(sfm_centers, name="sfm_centers")
    srt = as_points(srt_centers, name="srt_centers")
    if len(sfm) != len(srt):
        raise ValueError("sfm_centers and srt_centers must have the same length")
    if config.spatial_dedupe_m <= 0.0:
        raise ValueError("spatial_dedupe_m must be positive")
    if not 0.0 <= config.vertical_weight <= 1.0:
        raise ValueError("vertical_weight must be within [0, 1]")

    centered = srt - srt.mean(axis=0) if len(srt) else np.empty((0, 3))
    singular = np.linalg.svd(centered, compute_uv=False) if len(srt) else np.zeros(3)
    singular = np.pad(singular, (0, max(0, 3 - len(singular))))[:3]
    leading = float(singular[0])
    rank = int(np.count_nonzero(singular > max(1e-9, leading * config.rank_relative_threshold))) if leading else 0
    xy_singular = np.linalg.svd(srt[:, :2] - srt[:, :2].mean(axis=0), compute_uv=False) if len(srt) else np.zeros(2)
    xy_ratio = float(xy_singular[1] / xy_singular[0]) if len(xy_singular) > 1 and xy_singular[0] > 1e-9 else 0.0
    baseline = _xy_baseline(srt)
    distinct = _distinct_positions(srt, config.spatial_dedupe_m)
    reasons: list[str] = []
    if len(srt) < config.min_common_frames:
        reasons.append("insufficient-common-frames")
    if distinct < config.min_independent_positions:
        reasons.append("insufficient-independent-positions")
    if baseline < config.min_baseline_m:
        reasons.append("short-xy-baseline")
    if rank < 2:
        reasons.append("rank-below-two")
    if xy_ratio < config.linear_ratio_threshold:
        reasons.append("near-linear")
    vertical = "low" if rank == 2 else "normal" if rank >= 3 else "unobservable"
    return ReadinessReport(
        accepted=not reasons,
        common_frame_count=len(srt),
        distinct_position_count=distinct,
        xy_baseline_m=baseline,
        singular_values=tuple(float(value) for value in singular),
        spatial_rank=rank,
        vertical_observability=vertical,
        rejection_reasons=tuple(reasons),
    )
