"""Offline Rank-1 SfM/SRT constrained-alignment primitives.

This module is intentionally independent from the standard rank>=2 Sim3
implementation.  It never treats a line trajectory as a fully observable
position-only Sim3.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from cadscene.alignment.orientation_prior import (
    OrientationPriorPackage,
    PriorQualificationConfig,
    qualify_orientation_prior,
)
from cadscene.sfm.trajectory import SfmTrajectory
from cadscene.srt.fusion import rotate_cam_from_world_quat


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
    max_along_translation_spread_m: float = 2.0
    max_lateral_translation_spread_m: float = 2.0
    max_solve_height_offset_range_m: float = 3.0
    max_solve_height_offset_mad_m: float = 1.5
    min_direction_angle_deg: float = 20.0
    smoothing_window_sec: float = 2.0
    min_smoothing_support: int = 3
    vertical_smoothing_window_sec: float = 2.0
    min_vertical_smoothing_support: int = 3
    max_sfm_vertical_detail_m: float = 0.5
    max_validate_position_error_m: float = 5.0
    max_validate_orientation_error_deg: float = 15.0


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


@dataclass(frozen=True)
class Rank1Transform:
    scale: float
    rotation_cad_from_sfm: np.ndarray
    translation_cad_from_sfm: np.ndarray
    solve_frame_indices: tuple[int, ...]
    rotation_disagreement_deg: float
    along_translation_spread_m: float = 0.0
    lateral_translation_spread_m: float = 0.0
    height_offset_m: float = 0.0
    solve_height_offset_range_m: float = 0.0
    solve_height_offset_mad_m: float = 0.0

    def apply_points(self, points: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        values = np.asarray(points, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != 3 or len(values) < 1 or not np.isfinite(values).all():
            raise ValueError("points must have finite shape (N, 3) with N>=1")
        return self.scale * (values @ self.rotation_cad_from_sfm.T) + self.translation_cad_from_sfm


@dataclass(frozen=True)
class AlongTrackCorrection:
    base_positions: np.ndarray
    fused_positions: np.ndarray
    raw_delta_u_m: np.ndarray
    smoothed_delta_u_m: np.ndarray
    smoothing_support: np.ndarray
    srt_valid: np.ndarray


@dataclass(frozen=True)
class VerticalCorrection:
    base_positions: np.ndarray
    fused_positions: np.ndarray
    srt_relative_height_m: np.ndarray
    srt_height_valid: np.ndarray
    vertical_correction_m: np.ndarray
    vertical_smoothing_support: np.ndarray
    sfm_vertical_detail_m: np.ndarray


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


def cad_track_basis(
    d_cad: Sequence[float] | np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return horizontal along/lateral axes plus CAD Z without vertical leakage."""

    direction = np.asarray(d_cad, dtype=np.float64)
    if direction.shape != (3,) or not np.isfinite(direction).all():
        raise ValueError("d_cad must be a finite 3-vector")
    along = np.asarray([direction[0], direction[1], 0.0], dtype=np.float64)
    norm = float(np.linalg.norm(along))
    if norm <= 1e-9:
        raise ValueError("Rank-1 CAD direction has no observable horizontal projection")
    along /= norm
    vertical = np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
    lateral = np.cross(vertical, along)
    lateral /= np.linalg.norm(lateral)
    return along, lateral, vertical


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


def _proper_rotation(rotation: np.ndarray, name: str) -> np.ndarray:
    value = np.asarray(rotation, dtype=np.float64)
    if value.shape != (3, 3) or not np.isfinite(value).all():
        raise ValueError(f"{name} must be a finite 3x3 rotation")
    if not np.allclose(value.T @ value, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(value), 1.0, atol=1e-6):
        raise ValueError(f"{name} must be orthogonal with determinant +1")
    return value


def _rotation_angle_deg(first: np.ndarray, second: np.ndarray) -> float:
    relative = first @ second.T
    cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _mean_rotation(rotations: Sequence[np.ndarray]) -> np.ndarray:
    accumulator = np.sum(np.asarray(rotations, dtype=np.float64), axis=0)
    u, _singular, vt = np.linalg.svd(accumulator)
    result = u @ vt
    if np.linalg.det(result) < 0.0:
        u[:, -1] *= -1.0
        result = u @ vt
    return _proper_rotation(result, "averaged solve rotation")


def solve_rank1_transform(
    trajectory: SfmTrajectory,
    scale_fit: AlongTrackScaleFit,
    sfm_primary_direction: Sequence[float] | np.ndarray,
    priors: Sequence[OrientationPriorPackage],
    config: Rank1Config = Rank1Config(),
    *,
    srt_relative_height_by_frame: Mapping[int, float] | None = None,
) -> Rank1Transform:
    """Recover SfM→CAD pose from qualified manual solve anchors only."""

    if not config.min_scale <= scale_fit.scale <= config.max_scale:
        raise ValueError("Rank-1 scale is non-positive or out of range")
    sfm_direction = np.asarray(sfm_primary_direction, dtype=np.float64)
    if sfm_direction.shape != (3,) or not np.isfinite(sfm_direction).all() or np.linalg.norm(sfm_direction) <= 1e-12:
        raise ValueError("SfM primary direction must be a finite non-zero 3-vector")
    sfm_direction /= np.linalg.norm(sfm_direction)
    solve_priors = [prior for prior in priors if prior.solver_role == "solve"]
    if not solve_priors:
        raise ValueError("Rank-1 alignment requires a qualified solve prior")

    frame_to_index = {int(frame): index for index, frame in enumerate(trajectory.frames)}
    candidates: list[np.ndarray] = []
    source_centers: list[np.ndarray] = []
    for prior in solve_priors:
        frame = int(prior.source_frame_index)
        if frame not in frame_to_index:
            raise ValueError(f"solve prior frame {frame} is not registered in SfM trajectory")
        pose_index = frame_to_index[frame]
        r_cam_from_sfm = trajectory.query(frame)[1]
        candidate = _proper_rotation(
            np.asarray(prior.rotation_cad_from_camera) @ r_cam_from_sfm,
            f"solve rotation at frame {frame}",
        )
        d_cad = candidate @ sfm_direction
        qualification = qualify_orientation_prior(
            prior,
            d_cad,
            PriorQualificationConfig(min_direction_angle_deg=config.min_direction_angle_deg),
        )
        if not qualification.accepted:
            reasons = ", ".join(qualification.rejection_reasons)
            raise ValueError(f"solve prior at frame {frame} is not qualified ({reasons})")
        candidates.append(candidate)
        source_centers.append(np.asarray(trajectory.centers[pose_index], dtype=np.float64))

    disagreement = 0.0
    for index, first in enumerate(candidates):
        for second in candidates[index + 1 :]:
            disagreement = max(disagreement, _rotation_angle_deg(first, second))
    if disagreement > config.max_solve_rotation_disagreement_deg:
        raise ValueError(
            f"solve prior rotation disagreement is too large ({disagreement:.6g} deg)"
        )
    rotation = _mean_rotation(candidates)
    translation_candidates = np.asarray(
        [
            np.asarray(prior.position_cad_m, dtype=np.float64)
            - scale_fit.scale * (rotation @ center)
            for prior, center in zip(solve_priors, source_centers)
        ],
        dtype=np.float64,
    )
    along, lateral, _cad_up = cad_track_basis(rotation @ sfm_direction)
    along_values = translation_candidates @ along
    lateral_values = translation_candidates @ lateral
    along_median = float(np.median(along_values))
    lateral_median = float(np.median(lateral_values))
    along_spread = float(np.max(np.abs(along_values - along_median)))
    lateral_spread = float(np.max(np.abs(lateral_values - lateral_median)))
    if along_spread > config.max_along_translation_spread_m:
        raise ValueError(f"solve prior along translation spread is too large ({along_spread:.6g} m)")
    if lateral_spread > config.max_lateral_translation_spread_m:
        raise ValueError(f"solve prior lateral translation spread is too large ({lateral_spread:.6g} m)")

    height_offsets: list[float] = []
    for prior in solve_priors:
        if srt_relative_height_by_frame is None:
            relative_height = 0.0
        else:
            relative_height = srt_relative_height_by_frame.get(int(prior.source_frame_index))
            if relative_height is None or not np.isfinite(float(relative_height)):
                raise ValueError(
                    f"solve prior frame {prior.source_frame_index} has no valid SRT relative height"
                )
        height_offsets.append(float(prior.position_cad_m[2]) - float(relative_height))
    height_offset_array = np.asarray(height_offsets, dtype=np.float64)
    height_offset = float(np.median(height_offset_array))
    height_range = float(np.ptp(height_offset_array))
    height_mad = float(np.median(np.abs(height_offset_array - height_offset)))
    if height_range > config.max_solve_height_offset_range_m:
        raise ValueError(f"solve prior SRT height offset range is too large ({height_range:.6g} m)")
    if height_mad > config.max_solve_height_offset_mad_m:
        raise ValueError(f"solve prior SRT height offset MAD is too large ({height_mad:.6g} m)")

    translation = (
        along_median * along
        + lateral_median * lateral
        + float(np.median(translation_candidates[:, 2])) * _cad_up
    )
    if not np.isfinite(translation).all():
        raise ValueError("Rank-1 translation contains NaN or Inf")
    return Rank1Transform(
        scale=float(scale_fit.scale),
        rotation_cad_from_sfm=rotation,
        translation_cad_from_sfm=translation,
        solve_frame_indices=tuple(int(prior.source_frame_index) for prior in solve_priors),
        rotation_disagreement_deg=disagreement,
        along_translation_spread_m=along_spread,
        lateral_translation_spread_m=lateral_spread,
        height_offset_m=height_offset,
        solve_height_offset_range_m=height_range,
        solve_height_offset_mad_m=height_mad,
    )


def apply_along_track_correction(
    base_positions: Sequence[Sequence[float]] | np.ndarray,
    d_cad: Sequence[float] | np.ndarray,
    frame_times_sec: Sequence[float] | np.ndarray,
    target_along_track_m: Sequence[float] | np.ndarray,
    srt_valid: Sequence[bool] | np.ndarray,
    config: Rank1Config = Rank1Config(),
) -> AlongTrackCorrection:
    """Apply bounded median residuals strictly along the transformed track axis."""

    base = _points(base_positions, "base_positions")
    axis = np.asarray(d_cad, dtype=np.float64)
    times = np.asarray(frame_times_sec, dtype=np.float64)
    target = np.asarray(target_along_track_m, dtype=np.float64)
    valid = np.asarray(srt_valid, dtype=bool)
    if axis.shape != (3,) or not np.isfinite(axis).all() or np.linalg.norm(axis) <= 1e-12:
        raise ValueError("d_cad must be a finite non-zero 3-vector")
    axis /= np.linalg.norm(axis)
    if times.shape != (len(base),) or target.shape != (len(base),) or valid.shape != (len(base),):
        raise ValueError("times, target along-track values and validity must match base positions")
    if not np.isfinite(times).all() or not np.isfinite(target[valid]).all():
        raise ValueError("valid Rank-1 time/SRT samples must be finite")
    if config.smoothing_window_sec <= 0.0 or config.min_smoothing_support < 1:
        raise ValueError("Rank-1 smoothing settings must be positive")

    base_u = project_along_track(base, axis, base[0])
    raw_delta = np.full(len(base), np.nan, dtype=np.float64)
    raw_delta[valid] = target[valid] - base_u[valid]
    smooth = np.zeros(len(base), dtype=np.float64)
    support = np.zeros(len(base), dtype=np.int64)
    half_window = config.smoothing_window_sec / 2.0
    for index in range(len(base)):
        if not valid[index]:
            continue
        left = index
        while left > 0 and valid[left - 1]:
            left -= 1
        right = index
        while right + 1 < len(base) and valid[right + 1]:
            right += 1
        window = np.arange(left, right + 1)
        window = window[np.abs(times[window] - times[index]) <= half_window]
        support[index] = len(window)
        if len(window) >= config.min_smoothing_support:
            smooth[index] = float(np.median(raw_delta[window]))
    fused = base + smooth[:, None] * axis[None, :]
    return AlongTrackCorrection(
        base_positions=base,
        fused_positions=fused,
        raw_delta_u_m=raw_delta,
        smoothed_delta_u_m=smooth,
        smoothing_support=support,
        srt_valid=valid,
    )


def _segmented_time_median(
    values: np.ndarray,
    times: np.ndarray,
    valid: np.ndarray,
    *,
    window_sec: float,
    min_support: int,
) -> tuple[np.ndarray, np.ndarray]:
    low = np.full(len(values), np.nan, dtype=np.float64)
    support = np.zeros(len(values), dtype=np.int64)
    half_window = float(window_sec) / 2.0
    for index in range(len(values)):
        if not valid[index]:
            continue
        left = index
        while left > 0 and valid[left - 1]:
            left -= 1
        right = index
        while right + 1 < len(values) and valid[right + 1]:
            right += 1
        window = np.arange(left, right + 1)
        window = window[np.abs(times[window] - times[index]) <= half_window]
        window = window[np.isfinite(values[window])]
        support[index] = len(window)
        if len(window) >= min_support:
            low[index] = float(np.median(values[window]))
    return low, support


def apply_vertical_srt_constraint(
    base_positions: Sequence[Sequence[float]] | np.ndarray,
    frame_times_sec: Sequence[float] | np.ndarray,
    srt_relative_height_m: Sequence[float] | np.ndarray,
    srt_height_valid: Sequence[bool] | np.ndarray,
    *,
    height_offset_m: float,
    config: Rank1Config = Rank1Config(),
) -> VerticalCorrection:
    """Fuse SRT low-frequency relative height with bounded SfM vertical detail."""

    base = _points(base_positions, "base_positions")
    times = np.asarray(frame_times_sec, dtype=np.float64)
    relative = np.asarray(srt_relative_height_m, dtype=np.float64)
    valid = np.asarray(srt_height_valid, dtype=bool)
    if times.shape != (len(base),) or relative.shape != (len(base),) or valid.shape != (len(base),):
        raise ValueError("vertical times, SRT heights and validity must match base positions")
    if not np.isfinite(times).all() or not np.isfinite(relative[valid]).all():
        raise ValueError("valid SRT relative heights and frame times must be finite")
    if not np.isfinite(float(height_offset_m)):
        raise ValueError("height_offset_m must be finite")
    if config.vertical_smoothing_window_sec <= 0.0:
        raise ValueError("vertical_smoothing_window_sec must be positive")
    if config.min_vertical_smoothing_support < 1:
        raise ValueError("min_vertical_smoothing_support must be positive")
    if config.max_sfm_vertical_detail_m < 0.0:
        raise ValueError("max_sfm_vertical_detail_m must be non-negative")

    target = np.full(len(base), np.nan, dtype=np.float64)
    target[valid] = float(height_offset_m) + relative[valid]
    target_low, target_support = _segmented_time_median(
        target,
        times,
        valid,
        window_sec=config.vertical_smoothing_window_sec,
        min_support=config.min_vertical_smoothing_support,
    )
    sfm_low, sfm_support = _segmented_time_median(
        base[:, 2],
        times,
        valid,
        window_sec=config.vertical_smoothing_window_sec,
        min_support=config.min_vertical_smoothing_support,
    )
    applicable = valid & np.isfinite(target_low) & np.isfinite(sfm_low)
    detail = np.zeros(len(base), dtype=np.float64)
    detail[applicable] = np.clip(
        base[applicable, 2] - sfm_low[applicable],
        -float(config.max_sfm_vertical_detail_m),
        float(config.max_sfm_vertical_detail_m),
    )
    fused = base.copy()
    fused[applicable, 2] = target_low[applicable] + detail[applicable]
    correction = fused[:, 2] - base[:, 2]
    support = np.minimum(target_support, sfm_support)
    support[~applicable] = 0
    return VerticalCorrection(
        base_positions=base,
        fused_positions=fused,
        srt_relative_height_m=relative,
        srt_height_valid=valid,
        vertical_correction_m=correction,
        vertical_smoothing_support=support,
        sfm_vertical_detail_m=detail,
    )


def build_rank1_trajectory_json(
    raw_trajectory: dict,
    transform: Rank1Transform,
    corrected_positions: Sequence[Sequence[float]] | np.ndarray,
    correction: AlongTrackCorrection | None = None,
    vertical_correction: VerticalCorrection | None = None,
) -> dict:
    """Build a legacy-loader-compatible trajectory in CAD metres."""

    output = deepcopy(raw_trajectory)
    registered = [pose for pose in output.get("poses", []) if pose.get("registered", True)]
    positions = _points(corrected_positions, "corrected_positions")
    if len(registered) != len(positions):
        raise ValueError("registered trajectory poses do not match corrected positions")
    if correction is not None and len(correction.fused_positions) != len(positions):
        raise ValueError("Rank-1 correction does not match trajectory pose count")
    for index, pose in enumerate(registered):
        pose["center"] = [float(value) for value in positions[index]]
        pose["cam_from_world_quat_wxyz"] = rotate_cam_from_world_quat(
            pose["cam_from_world_quat_wxyz"],
            transform.rotation_cad_from_sfm,
        )
        pose["trajectory_source"] = "rank1_sfm_srt_along_track_cad"
        if correction is not None:
            pose["srt_valid"] = bool(correction.srt_valid[index])
            pose["along_track_correction_m"] = float(correction.smoothed_delta_u_m[index])
            pose["smoothing_support"] = int(correction.smoothing_support[index])
        if vertical_correction is not None:
            pose["srt_height_valid"] = bool(vertical_correction.srt_height_valid[index])
            pose["srt_relative_height_m"] = (
                float(vertical_correction.srt_relative_height_m[index])
                if vertical_correction.srt_height_valid[index]
                else None
            )
            pose["vertical_correction_m"] = float(vertical_correction.vertical_correction_m[index])
            pose["vertical_smoothing_support"] = int(vertical_correction.vertical_smoothing_support[index])
            pose["sfm_vertical_detail_m"] = float(vertical_correction.sfm_vertical_detail_m[index])
    output["meta"] = {
        **dict(output.get("meta") or {}),
        "trajectory_mode": "srt_rank1_manual_prior",
        "coordinate_system": "cad_meters",
        "position_source": "rank1_sfm_srt_along_track_cad",
        "orientation_source": "sfm_plus_manual_prior",
        "srt_constraint": "along_track_only",
        "transform_name": "rank1_sfm_to_cad_base",
        "scale": float(transform.scale),
        "rotation_cad_from_sfm": transform.rotation_cad_from_sfm.tolist(),
        "translation_cad_from_sfm": transform.translation_cad_from_sfm.tolist(),
    }
    if vertical_correction is not None:
        output["meta"].update(
            {
                "srt_constraint": "along_track_and_relative_height",
                "vertical_source": "srt_relative_altitude_primary",
                "height_datum": "manual_solve_anchors",
                "absolute_elevation_available": False,
            }
        )
    return output
