"""Adjusted Bentley AT track interpolation and calibrated segment projection."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Mapping, Sequence

import numpy as np
from pyproj import Transformer
from scipy.spatial.transform import Rotation, Slerp

from cadscene.core.camera import CameraState
from cadscene.srt.bentley_pose_merge import BentleyCameraModel, BentleyPoseSample
from cadscene.srt.georeference import (
    CadGeoreference,
    cad_raw_to_local_m,
    project_wgs84_to_cad_raw,
)


@dataclass(frozen=True)
class AtFramePose:
    frame_index: int
    camera: CameraState
    longitude: float
    latitude: float
    adjusted_altitude_m: float


@dataclass(frozen=True)
class DistortedProjection:
    uv_starts: np.ndarray
    uv_ends: np.ndarray
    depth_starts: np.ndarray
    depth_ends: np.ndarray


def _record_value(record: object, name: str) -> object | None:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def srt_vertical_reference_m(records: Sequence[object]) -> float:
    """Estimate the absolute-height datum represented by SRT relative altitude."""

    offsets: list[float] = []
    for record in records:
        absolute = _record_value(record, "abs_alt")
        relative = _record_value(record, "rel_alt")
        if absolute is None or relative is None:
            continue
        offset = float(absolute) - float(relative)
        if isfinite(offset):
            offsets.append(offset)
    if not offsets:
        raise ValueError("SRT has no absolute/relative altitude pairs")
    return float(median(offsets))


def _validated_samples(
    samples: Sequence[BentleyPoseSample],
) -> tuple[BentleyPoseSample, ...]:
    ordered = tuple(sorted(samples, key=lambda item: item.frame_index))
    if not ordered:
        raise ValueError("Bentley adjusted pose samples must not be empty")
    if any(
        first.frame_index == second.frame_index
        for first, second in zip(ordered, ordered[1:])
    ):
        raise ValueError("Bentley adjusted pose samples require unique frame indices")
    for sample in ordered:
        center = sample.adjusted_lon_lat_alt
        if center is None or len(center) != 3 or not all(isfinite(float(v)) for v in center):
            raise ValueError("every Bentley pose sample requires an adjusted XML center")
    return ordered


def _interpolated_centers_ecef(
    samples: Sequence[BentleyPoseSample], frame_indices: np.ndarray
) -> np.ndarray:
    to_ecef = Transformer.from_crs(4979, 4978, always_xy=True)
    from_ecef = Transformer.from_crs(4978, 4979, always_xy=True)
    centers = np.asarray(
        [sample.adjusted_lon_lat_alt for sample in samples], dtype=np.float64
    )
    ecef = np.column_stack(to_ecef.transform(centers[:, 0], centers[:, 1], centers[:, 2]))
    anchors = np.asarray([sample.frame_index for sample in samples], dtype=np.float64)
    interpolated = np.column_stack(
        [np.interp(frame_indices, anchors, ecef[:, axis]) for axis in range(3)]
    )
    longitude, latitude, altitude = from_ecef.transform(
        interpolated[:, 0], interpolated[:, 1], interpolated[:, 2]
    )
    return np.column_stack((longitude, latitude, altitude))


def _interpolated_rotations(
    samples: Sequence[BentleyPoseSample], frame_indices: np.ndarray
) -> Rotation:
    rotations = Rotation.from_euler(
        "ZYX",
        [[sample.yaw_deg, sample.pitch_deg, sample.roll_deg] for sample in samples],
        degrees=True,
    )
    if len(samples) == 1:
        return Rotation.from_matrix(
            np.repeat(rotations.as_matrix()[0][None, :, :], len(frame_indices), axis=0)
        )
    anchors = np.asarray([sample.frame_index for sample in samples], dtype=np.float64)
    query = np.clip(frame_indices, anchors[0], anchors[-1])
    return Slerp(anchors, rotations)(query)


def build_adjusted_at_track(
    samples: Sequence[BentleyPoseSample],
    frame_indices: Sequence[int],
    *,
    georeference: CadGeoreference,
    cad_origin_xy: Sequence[float],
    cad_scale: float,
    vertical_reference_m: float,
    fov_deg: float,
) -> tuple[AtFramePose, ...]:
    """Interpolate adjusted XML centers/rotations at requested source frames."""

    ordered = _validated_samples(samples)
    requested = np.asarray([int(value) for value in frame_indices], dtype=np.float64)
    if not len(requested):
        return ()
    if not isfinite(float(vertical_reference_m)):
        raise ValueError("vertical_reference_m must be finite")
    if not isfinite(float(fov_deg)) or not 1.0 < float(fov_deg) < 179.0:
        raise ValueError("fov_deg must be finite and inside (1, 179)")

    centers = _interpolated_centers_ecef(ordered, requested)
    eulers = _interpolated_rotations(ordered, requested).as_euler("ZYX", degrees=True)
    result: list[AtFramePose] = []
    for frame_value, center, euler in zip(requested, centers, eulers):
        longitude, latitude, altitude = (float(value) for value in center)
        cad_raw = project_wgs84_to_cad_raw(longitude, latitude, georeference)
        camera_x, camera_y = cad_raw_to_local_m(cad_raw, cad_origin_xy, cad_scale)
        yaw, xml_pitch, roll = (float(value) for value in euler)
        result.append(
            AtFramePose(
                frame_index=int(frame_value),
                camera=CameraState(
                    camera_x=camera_x,
                    camera_y=camera_y,
                    camera_z=altitude - float(vertical_reference_m),
                    yaw_deg=yaw,
                    pitch_deg=-xml_pitch,
                    roll_deg=roll,
                    fov_deg=float(fov_deg),
                    cad_scale=float(cad_scale),
                ),
                longitude=longitude,
                latitude=latitude,
                adjusted_altitude_m=altitude,
            )
        )
    return tuple(result)


def _project_points(points: np.ndarray, model: BentleyCameraModel) -> tuple[np.ndarray, np.ndarray]:
    values = np.asarray(points, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 3:
        raise ValueError("camera points must have shape (N, 3)")
    if not np.all(np.isfinite(values)):
        raise ValueError("camera points must be finite")
    depth = values[:, 2].copy()
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        x = values[:, 0] / depth
        y = values[:, 1] / depth
        r2 = x * x + y * y
        k1, k2, k3, p1, p2 = model.distortion
        radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        fx, fy = model.focal_pixels
        cx, cy = model.principal_point_px
        uv = np.column_stack(
            (cx + fx * xd + model.skew * yd, cy + fy * yd)
        )
    return uv, depth


def project_distorted_segments(
    starts_camera: np.ndarray,
    ends_camera: np.ndarray,
    model: BentleyCameraModel,
) -> DistortedProjection:
    """Project paired camera-space endpoints with XML Brown-Conrady calibration."""

    starts = np.asarray(starts_camera, dtype=np.float64)
    ends = np.asarray(ends_camera, dtype=np.float64)
    if starts.shape != ends.shape:
        raise ValueError("segment endpoint arrays must have the same shape")
    uv_starts, depth_starts = _project_points(starts, model)
    uv_ends, depth_ends = _project_points(ends, model)
    return DistortedProjection(
        uv_starts=uv_starts,
        uv_ends=uv_ends,
        depth_starts=depth_starts,
        depth_ends=depth_ends,
    )
