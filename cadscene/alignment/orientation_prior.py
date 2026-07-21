"""Manual orientation-prior construction and qualification only.

No Rank-1 transform is estimated here.  The module simply makes provenance,
pose conventions, and hold-out eligibility explicit for a future solver.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from cadscene.alignment.aligner import _camera_to_world_rotation
from cadscene.core.coordinates import web_camera_to_python_state


_SUPPORTED_DIRECTIONS = {"camera_forward", "camera_up", "camera_right", "cad_up", "ground_normal"}


@dataclass(frozen=True)
class OrientationPriorPackage:
    source_frame_index: int
    pts_time_sec: float
    position_cad_m: np.ndarray
    rotation_cad_from_camera: np.ndarray
    camera_forward_cad: np.ndarray
    camera_up_cad: np.ndarray
    camera_right_cad: np.ndarray
    direction_type: str | None
    direction_vector_cad: np.ndarray
    orientation_source: str
    orientation_confirmed: bool
    yaw_confirmed: bool
    pitch_confirmed: bool
    roll_confirmed: bool
    solver_role: str
    projection_residual_px: float | None
    qualification: bool = False
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriorQualificationConfig:
    min_direction_angle_deg: float = 20.0
    allowed_orientation_sources: tuple[str, ...] = ("manual",)


@dataclass(frozen=True)
class PriorQualification:
    accepted: bool
    angle_to_axis_deg: float | None
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PriorSplitReport:
    solve_count: int
    validate_count: int
    duplicate_frame_indices: tuple[int, ...]
    accepted: bool
    rejection_reasons: tuple[str, ...]


def _unit(vector: np.ndarray) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(value))
    if not np.isfinite(value).all() or not math.isfinite(norm) or norm <= 1e-12:
        return value
    return value / norm


def _direction_vector(direction_type: str | None, *, right: np.ndarray, up: np.ndarray, forward: np.ndarray) -> np.ndarray:
    if direction_type == "camera_forward":
        return forward
    if direction_type == "camera_up":
        return up
    if direction_type == "camera_right":
        return right
    if direction_type == "cad_up":
        return np.asarray([0.0, 0.0, 1.0], dtype=np.float64)
    return np.asarray([math.nan, math.nan, math.nan], dtype=np.float64)


def build_orientation_prior(
    keyframe: Mapping[str, Any],
    *,
    origin_xy: tuple[float, float] = (0.0, 0.0),
    cad_scale: float = 1.0,
) -> OrientationPriorPackage:
    """Build a CAD-world prior using the established web-to-Python convention."""

    camera = dict(keyframe.get("camera") or {})
    state = web_camera_to_python_state(camera, origin_xy=origin_xy, cad_scale=cad_scale)
    rotation = np.asarray(_camera_to_world_rotation(state), dtype=np.float64)
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-8) or not np.isclose(np.linalg.det(rotation), 1.0, atol=1e-8):
        raise ValueError("camera rotation is not a proper orthogonal matrix")
    right = _unit(rotation[:, 0])
    # Existing camera axes are right/down/forward; therefore camera up is -down.
    up = _unit(-rotation[:, 1])
    forward = _unit(rotation[:, 2])
    metadata = dict(keyframe.get("orientation_metadata") or {})
    prior = dict(keyframe.get("prior") or {})
    source = str(keyframe.get("source") or "")
    orientation_source = "algorithm" if source == "algorithm_prediction" else str(metadata.get("orientation_source") or "manual")
    direction_type = prior.get("direction_type") if bool(prior.get("enabled", False)) else None
    return OrientationPriorPackage(
        source_frame_index=int(keyframe.get("source_frame_index", keyframe.get("frame", -1))),
        pts_time_sec=float(keyframe.get("pts_time_sec", keyframe.get("time", math.nan))),
        position_cad_m=np.asarray([state.camera_x, state.camera_y, state.camera_z], dtype=np.float64),
        rotation_cad_from_camera=rotation,
        camera_forward_cad=forward,
        camera_up_cad=up,
        camera_right_cad=right,
        direction_type=str(direction_type) if direction_type is not None else None,
        direction_vector_cad=_unit(_direction_vector(str(direction_type) if direction_type is not None else None, right=right, up=up, forward=forward)),
        orientation_source=orientation_source,
        orientation_confirmed=bool(metadata.get("orientation_confirmed", False)),
        yaw_confirmed=bool(metadata.get("yaw_confirmed", False)),
        pitch_confirmed=bool(metadata.get("pitch_confirmed", False)),
        roll_confirmed=bool(metadata.get("roll_confirmed", False)),
        solver_role=str(prior.get("solver_role") or "none"),
        projection_residual_px=metadata.get("projection_residual_px"),
    )


def qualify_orientation_prior(
    prior: OrientationPriorPackage,
    trajectory_direction_cad: np.ndarray,
    config: PriorQualificationConfig = PriorQualificationConfig(),
) -> PriorQualification:
    """Evaluate a candidate using the undirected trajectory-axis angle in degrees."""

    reasons: list[str] = []
    if prior.source_frame_index < 0:
        reasons.append("source-frame-index-missing")
    if not math.isfinite(prior.pts_time_sec):
        reasons.append("pts-time-missing")
    if not np.isfinite(prior.position_cad_m).all() or not np.isfinite(prior.rotation_cad_from_camera).all():
        reasons.append("camera-pose-not-finite")
    if prior.orientation_source not in config.allowed_orientation_sources:
        reasons.append("orientation-source-not-allowed")
    if not prior.orientation_confirmed:
        reasons.append("orientation-not-confirmed")
    if prior.direction_type not in _SUPPORTED_DIRECTIONS:
        reasons.append("direction-type-not-supported")
    if prior.direction_type == "camera_forward" and (not prior.yaw_confirmed or not prior.pitch_confirmed):
        reasons.append("forward-yaw-pitch-not-confirmed")
    if prior.direction_type in {"camera_up", "camera_right"} and not prior.roll_confirmed:
        reasons.append("roll-not-confirmed")
    if prior.direction_type == "cad_up":
        reasons.append("cad-up-metadata-only")
    if prior.direction_type == "ground_normal":
        reasons.append("ground-normal-missing-versioned-source")
    if prior.solver_role not in {"solve", "validate"}:
        reasons.append("solver-role-not-eligible")

    direction = np.asarray(prior.direction_vector_cad, dtype=np.float64)
    trajectory = np.asarray(trajectory_direction_cad, dtype=np.float64)
    if not np.isfinite(direction).all() or not np.isfinite(trajectory).all():
        reasons.append("direction-not-finite")
        return PriorQualification(False, None, tuple(dict.fromkeys(reasons)))
    direction_norm = float(np.linalg.norm(direction))
    trajectory_norm = float(np.linalg.norm(trajectory))
    if direction_norm <= 1e-12 or trajectory_norm <= 1e-12:
        reasons.append("direction-not-unit-or-zero")
        return PriorQualification(False, None, tuple(dict.fromkeys(reasons)))
    if not np.isclose(direction_norm, 1.0, atol=1e-6):
        reasons.append("direction-not-unit-or-zero")
    unit_dot = abs(float(np.dot(direction / direction_norm, trajectory / trajectory_norm)))
    angle_to_axis = math.degrees(math.acos(float(np.clip(unit_dot, -1.0, 1.0))))
    if angle_to_axis < float(config.min_direction_angle_deg):
        reasons.append("prior-near-parallel-to-trajectory")
    return PriorQualification(not reasons, angle_to_axis, tuple(dict.fromkeys(reasons)))


def validate_prior_split(priors: Iterable[OrientationPriorPackage]) -> PriorSplitReport:
    """Validate the hold-out protocol independently of a future solver."""

    rows = list(priors)
    solve_frames = {item.source_frame_index for item in rows if item.solver_role == "solve"}
    validate_frames = {item.source_frame_index for item in rows if item.solver_role == "validate"}
    duplicates = tuple(sorted(solve_frames & validate_frames))
    reasons: list[str] = []
    if not solve_frames:
        reasons.append("missing-solve-prior")
    if not validate_frames:
        reasons.append("missing-validate-anchor")
    if duplicates:
        reasons.append("solve-validate-frame-overlap")
    return PriorSplitReport(
        solve_count=len(solve_frames),
        validate_count=len(validate_frames),
        duplicate_frame_indices=duplicates,
        accepted=not reasons,
        rejection_reasons=tuple(reasons),
    )
