"""Joint no-XML alignment of COLMAP poses to SRT and guarded DJI priors."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from cadscene.srt.colmap_pose_transfer import _umeyama


@dataclass(frozen=True)
class JointAlignmentResult:
    success: bool
    message: str
    scale: float
    world_rotation: np.ndarray
    translation: np.ndarray
    installation_rotation: np.ndarray
    corrected_centers: np.ndarray
    world_from_camera: np.ndarray
    knot_times_sec: np.ndarray
    knot_offsets_m: np.ndarray
    initial_cost: float
    final_cost: float
    nfev: int


def _robust_umeyama(source: np.ndarray, target: np.ndarray):
    active = np.arange(len(source), dtype=np.int64)
    result = _umeyama(source, target)
    for _ in range(4):
        errors = np.linalg.norm(result.apply(source) - target, axis=1)
        median = float(np.median(errors[active]))
        mad = float(np.median(np.abs(errors[active] - median)))
        threshold = median + max(1.0, 4.5 * 1.4826 * mad)
        candidate = np.flatnonzero(errors <= threshold)
        if len(candidate) < 3 or len(candidate) == len(active):
            break
        active = candidate
        result = _umeyama(source[active], target[active])
    return result


def _knot_times(frames: np.ndarray, frame_rate: float, interval_sec: float) -> np.ndarray:
    times = (frames - frames[0]).astype(np.float64) / frame_rate
    duration = float(times[-1])
    if duration <= 0.0:
        raise ValueError("joint alignment frames must span positive time")
    return np.unique(np.concatenate((np.arange(0.0, duration, interval_sec), [duration])))


def _interpolate_knots(frames, frame_rate, knot_times, knot_offsets):
    times = (frames - frames[0]).astype(np.float64) / frame_rate
    return np.column_stack([np.interp(times, knot_times, knot_offsets[:, axis]) for axis in range(3)])


def solve_joint_alignment(
    *, frames: np.ndarray, colmap_centers: np.ndarray,
    colmap_world_from_camera: np.ndarray, srt_centers: np.ndarray,
    dji_world_from_camera: np.ndarray | None, frame_rate: float,
    knot_interval_sec: float = 20.0,
) -> JointAlignmentResult:
    frame_values = np.asarray(frames, dtype=np.int64)
    centers = np.asarray(colmap_centers, dtype=np.float64)
    visual = np.asarray(colmap_world_from_camera, dtype=np.float64)
    targets = np.asarray(srt_centers, dtype=np.float64)
    dji = None if dji_world_from_camera is None else np.asarray(dji_world_from_camera, dtype=np.float64)
    count = len(frame_values)
    if count < 3 or np.any(np.diff(frame_values) <= 0):
        raise ValueError("joint alignment needs three strictly increasing frames")
    if centers.shape != (count, 3) or targets.shape != (count, 3):
        raise ValueError("center tracks must have shape (N, 3)")
    if visual.shape != (count, 3, 3) or (dji is not None and dji.shape != (count, 3, 3)):
        raise ValueError("rotation tracks must have shape (N, 3, 3)")
    if not np.isfinite(frame_rate) or frame_rate <= 0 or knot_interval_sec <= 0:
        raise ValueError("frame rate and knot interval must be positive")

    initial = _robust_umeyama(centers, targets)
    knot_times = _knot_times(frame_values, frame_rate, knot_interval_sec)
    origin = np.mean(targets, axis=0)
    x0 = np.concatenate(([np.log(initial.scale)], Rotation.from_matrix(initial.rotation).as_rotvec(), initial.translation - origin, np.zeros(len(knot_times) * 3)))

    def unpack(parameters):
        return (
            float(np.exp(parameters[0])),
            Rotation.from_rotvec(parameters[1:4]).as_matrix(),
            parameters[4:7] + origin,
            parameters[7:].reshape(-1, 3),
        )

    def residuals(parameters):
        scale, world, translation, knots = unpack(parameters)
        offsets = _interpolate_knots(frame_values, frame_rate, knot_times, knots)
        corrected = scale * (world @ centers.T).T + translation + offsets
        values = [((corrected - targets) / 3.0).reshape(-1)]
        if dji is not None:
            camera = world[None, :, :] @ visual
            delta = dji.transpose(0, 2, 1) @ camera
            values.append((Rotation.from_matrix(delta).as_rotvec() / np.radians(2.0)).reshape(-1))
        if len(knots) > 2:
            values.append((knots[:-2] - 2 * knots[1:-1] + knots[2:]).reshape(-1))
        values.extend((knots.reshape(-1) / 0.5, knots[[0, -1]].reshape(-1) / 2.0))
        return np.concatenate(values)

    initial_residual = residuals(x0)
    solution = least_squares(residuals, x0, loss="soft_l1", f_scale=1.0, x_scale="jac", max_nfev=500)
    scale, world, translation, knots = unpack(solution.x)
    offsets = _interpolate_knots(frame_values, frame_rate, knot_times, knots)
    final_residual = residuals(solution.x)
    return JointAlignmentResult(
        success=bool(solution.success), message=str(solution.message), scale=scale,
        world_rotation=world, translation=translation, installation_rotation=np.eye(3),
        corrected_centers=scale * (world @ centers.T).T + translation + offsets,
        world_from_camera=world[None, :, :] @ visual,
        knot_times_sec=knot_times, knot_offsets_m=knots,
        initial_cost=float(0.5 * np.dot(initial_residual, initial_residual)),
        final_cost=float(0.5 * np.dot(final_residual, final_residual)), nfev=int(solution.nfev),
    )
