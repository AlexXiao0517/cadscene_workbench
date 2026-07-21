"""Independent hold-out validation for Rank-1 SfM→CAD alignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from cadscene.alignment.orientation_prior import (
    OrientationPriorPackage,
    PriorQualificationConfig,
    qualify_orientation_prior,
)
from cadscene.alignment.rank1_constrained import Rank1Config, Rank1Transform
from cadscene.sfm.trajectory import SfmTrajectory


@dataclass(frozen=True)
class HoldoutAnchorMetric:
    source_frame_index: int
    position_error_m: float
    along_track_error_m: float
    cross_track_error_m: float
    vertical_error_m: float
    orientation_error_deg: float
    forward_angle_error_deg: float
    up_angle_error_deg: float
    right_angle_error_deg: float
    projection_residual_px: float | None


@dataclass(frozen=True)
class Rank1ValidationReport:
    accepted: bool
    holdout_missing: bool
    metrics: tuple[HoldoutAnchorMetric, ...]
    rejection_reasons: tuple[str, ...]


def _angle_deg(first: np.ndarray, second: np.ndarray) -> float:
    a = np.asarray(first, dtype=np.float64)
    b = np.asarray(second, dtype=np.float64)
    a /= max(float(np.linalg.norm(a)), 1e-12)
    b /= max(float(np.linalg.norm(b)), 1e-12)
    return float(np.degrees(np.arccos(float(np.clip(np.dot(a, b), -1.0, 1.0)))))


def _rotation_angle_deg(first: np.ndarray, second: np.ndarray) -> float:
    relative = np.asarray(first, dtype=np.float64) @ np.asarray(second, dtype=np.float64).T
    cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def validate_holdout_anchors(
    trajectory: SfmTrajectory,
    predicted_centers_cad: Sequence[Sequence[float]] | np.ndarray,
    transform: Rank1Transform,
    d_cad: Sequence[float] | np.ndarray,
    priors: Sequence[OrientationPriorPackage],
    config: Rank1Config = Rank1Config(),
) -> Rank1ValidationReport:
    """Evaluate validate anchors without estimating or mutating any transform."""

    predicted = np.asarray(predicted_centers_cad, dtype=np.float64)
    if predicted.shape != trajectory.centers.shape or not np.isfinite(predicted).all():
        raise ValueError("predicted CAD centers must match registered SfM trajectory")
    axis = np.asarray(d_cad, dtype=np.float64)
    if axis.shape != (3,) or not np.isfinite(axis).all() or np.linalg.norm(axis) <= 1e-12:
        raise ValueError("d_cad must be a finite non-zero 3-vector")
    axis /= np.linalg.norm(axis)
    validate_priors = [prior for prior in priors if prior.solver_role == "validate"]
    if not validate_priors:
        return Rank1ValidationReport(False, True, (), ("holdout-missing",))

    frame_to_index = {int(frame): index for index, frame in enumerate(trajectory.frames)}
    cad_up = np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
    cross_axis = np.cross(cad_up, axis)
    if np.linalg.norm(cross_axis) <= 1e-9:
        # A near-vertical Rank-1 path has no stable horizontal cross-track axis.
        cross_axis = np.asarray([1.0, 0.0, 0.0], dtype=np.float64)
    cross_axis /= np.linalg.norm(cross_axis)
    metrics: list[HoldoutAnchorMetric] = []
    reasons: list[str] = []
    for prior in validate_priors:
        frame = int(prior.source_frame_index)
        if frame not in frame_to_index:
            reasons.append(f"validate-frame-not-registered:{frame}")
            continue
        qualification = qualify_orientation_prior(
            prior,
            axis,
            PriorQualificationConfig(min_direction_angle_deg=config.min_direction_angle_deg),
        )
        if not qualification.accepted:
            reasons.append(f"validate-prior-not-qualified:{frame}")
            continue
        index = frame_to_index[frame]
        error = predicted[index] - np.asarray(prior.position_cad_m, dtype=np.float64)
        r_cam_from_sfm = trajectory.query(frame)[1]
        r_cam_from_cad = r_cam_from_sfm @ transform.rotation_cad_from_sfm.T
        predicted_cad_from_camera = r_cam_from_cad.T
        manual = np.asarray(prior.rotation_cad_from_camera, dtype=np.float64)
        metric = HoldoutAnchorMetric(
            source_frame_index=frame,
            position_error_m=float(np.linalg.norm(error)),
            along_track_error_m=float(np.dot(error, axis)),
            cross_track_error_m=float(np.dot(error, cross_axis)),
            vertical_error_m=float(error[2]),
            orientation_error_deg=_rotation_angle_deg(predicted_cad_from_camera, manual),
            forward_angle_error_deg=_angle_deg(predicted_cad_from_camera[:, 2], manual[:, 2]),
            up_angle_error_deg=_angle_deg(-predicted_cad_from_camera[:, 1], -manual[:, 1]),
            right_angle_error_deg=_angle_deg(predicted_cad_from_camera[:, 0], manual[:, 0]),
            projection_residual_px=prior.projection_residual_px,
        )
        metrics.append(metric)
        if metric.position_error_m > config.max_validate_position_error_m:
            reasons.append(f"validate-position-error:{frame}")
        if metric.orientation_error_deg > config.max_validate_orientation_error_deg:
            reasons.append(f"validate-orientation-error:{frame}")
    if not metrics:
        reasons.append("no-qualified-validate-anchor")
    return Rank1ValidationReport(
        accepted=not reasons,
        holdout_missing=False,
        metrics=tuple(metrics),
        rejection_reasons=tuple(dict.fromkeys(reasons)),
    )
