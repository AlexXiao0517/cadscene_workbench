from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from cadscene.cad.loader import CadBundle, RoadLine
from cadscene.core.camera import CameraState


@dataclass(frozen=True)
class CenterlineModel:
    dense: np.ndarray
    cum: np.ndarray

    @classmethod
    def from_points(cls, points: Sequence[Sequence[float]]) -> "CenterlineModel":
        dense = np.asarray(points, dtype=np.float64)
        if dense.ndim != 2 or dense.shape[1] < 2:
            raise ValueError("centerline points 必须是 Nx2 或 Nx3。")
        dense = dense[:, :2]
        if len(dense) == 0:
            cum = np.zeros(0, dtype=np.float64)
        else:
            seg = np.linalg.norm(np.diff(dense, axis=0), axis=1)
            cum = np.concatenate([[0.0], np.cumsum(seg)])
        return cls(dense=dense, cum=cum)

    @classmethod
    def from_road_line(cls, line: RoadLine) -> "CenterlineModel":
        return cls.from_points(line.points)


def project_station_lateral(
    center: CenterlineModel,
    cad_xy: Sequence[float] | np.ndarray,
    allow_extension: bool = True,
) -> tuple[float, float, int]:
    dense = np.asarray(center.dense, dtype=np.float64)
    cum = np.asarray(center.cum, dtype=np.float64)
    p = np.asarray(cad_xy, dtype=np.float64).reshape(2)
    if len(dense) < 2:
        return 0.0, 0.0, 0

    best_dist = float("inf")
    best_s = 0.0
    best_d = 0.0
    best_i = 0
    for i in range(len(dense) - 1):
        a = dense[i]
        b = dense[i + 1]
        v = b - a
        seg_len = float(np.linalg.norm(v))
        if seg_len <= 1e-9:
            continue
        raw_t = float(np.dot(p - a, v) / (seg_len * seg_len))
        if allow_extension and (i == 0 or i == len(dense) - 2):
            t = raw_t
        else:
            t = float(np.clip(raw_t, 0.0, 1.0))
        proj = a + t * v
        rel = p - proj
        dist = float(np.dot(rel, rel))
        if dist < best_dist:
            tangent = v / seg_len
            best_dist = dist
            best_s = float(cum[i] + t * seg_len)
            best_d = float(tangent[0] * rel[1] - tangent[1] * rel[0])
            best_i = i
    return best_s, best_d, best_i


def station_lateral_to_cad_xy(center: CenterlineModel, station_s: float, lateral_d: float) -> np.ndarray:
    dense = np.asarray(center.dense, dtype=np.float64)
    cum = np.asarray(center.cum, dtype=np.float64)
    if len(dense) < 2:
        return np.zeros(2, dtype=np.float64)

    s = float(station_s)
    if s <= float(cum[0]):
        i = 0
    elif s >= float(cum[-1]):
        i = len(dense) - 2
    else:
        i = int(np.searchsorted(cum, s, side="right") - 1)
        i = max(0, min(i, len(dense) - 2))

    a = dense[i]
    b = dense[i + 1]
    v = b - a
    seg_len = float(np.linalg.norm(v))
    if seg_len <= 1e-9:
        return a.copy()
    t = (s - float(cum[i])) / seg_len
    tangent = v / seg_len
    normal = np.array([-tangent[1], tangent[0]], dtype=np.float64)
    return a + t * v + float(lateral_d) * normal


def camera_state_cad_xy(bundle: CadBundle, state: CameraState) -> np.ndarray:
    scale = float(state.cad_scale) if state.cad_scale else 1.0
    return np.array(
        [
            float(bundle.origin_xy[0]) + float(state.camera_x) / scale,
            float(bundle.origin_xy[1]) + float(state.camera_y) / scale,
        ],
        dtype=np.float64,
    )


def cad_xy_to_camera_xy(bundle: CadBundle, cad_xy: Sequence[float], cad_scale: float) -> np.ndarray:
    p = np.asarray(cad_xy, dtype=np.float64).reshape(2)
    origin = np.asarray(bundle.origin_xy, dtype=np.float64)
    return (p - origin) * float(cad_scale)

