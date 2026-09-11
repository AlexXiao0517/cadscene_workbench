from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
import math

import numpy as np
from scipy.spatial import cKDTree

from .tpkg import TerrainControlSet, sampled_control_points


@dataclass(frozen=True)
class DrapedCadSegments:
    starts_xyz: np.ndarray
    ends_xyz: np.ndarray
    colors_bgr: np.ndarray
    source_segment_indices: np.ndarray
    control_distance_m: np.ndarray
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class TerrainConflict:
    source_ids: tuple[str, str]
    horizontal_distance_m: float
    vertical_difference_m: float
    midpoint_xy: tuple[float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_ids": list(self.source_ids),
            "horizontal_distance_m": self.horizontal_distance_m,
            "vertical_difference_m": self.vertical_difference_m,
            "midpoint_xy": list(self.midpoint_xy),
        }


def drape_cad_segments(
    cad_starts_xy: np.ndarray,
    cad_ends_xy: np.ndarray,
    colors_bgr: np.ndarray,
    controls: TerrainControlSet,
    *,
    sample_step_m: float = 2.0,
    max_distance_m: float = 160.0,
) -> DrapedCadSegments:
    starts = np.asarray(cad_starts_xy, dtype=np.float64).reshape(-1, 2)
    ends = np.asarray(cad_ends_xy, dtype=np.float64).reshape(-1, 2)
    colors = np.asarray(colors_bgr, dtype=np.uint8).reshape(-1, 3)
    if len(starts) != len(ends) or len(starts) != len(colors):
        raise ValueError("CAD segment arrays must have equal length")
    if not all(np.isfinite(value).all() for value in (starts, ends)):
        raise ValueError("CAD segment coordinates must be finite")
    if not math.isfinite(max_distance_m) or max_distance_m <= 0.0:
        raise ValueError("terrain control distance must be positive")
    samples, sample_sources = sampled_control_points(
        controls, step_m=sample_step_m
    )
    if not len(samples):
        return DrapedCadSegments(
            starts_xyz=np.empty((0, 3), dtype=np.float64),
            ends_xyz=np.empty((0, 3), dtype=np.float64),
            colors_bgr=np.empty((0, 3), dtype=np.uint8),
            source_segment_indices=np.empty((0,), dtype=np.int64),
            control_distance_m=np.empty((0, 2), dtype=np.float64),
            source_ids=(),
        )
    tree = cKDTree(samples[:, :2])
    start_distance, start_index = tree.query(starts, k=1)
    end_distance, end_index = tree.query(ends, k=1)
    keep = (start_distance <= max_distance_m) & (end_distance <= max_distance_m)
    selected = np.flatnonzero(keep)
    return DrapedCadSegments(
        starts_xyz=np.column_stack((starts[selected], samples[start_index[selected], 2])),
        ends_xyz=np.column_stack((ends[selected], samples[end_index[selected], 2])),
        colors_bgr=colors[selected],
        source_segment_indices=selected.astype(np.int64),
        control_distance_m=np.column_stack((start_distance[selected], end_distance[selected])),
        source_ids=tuple(sample_sources[int(index)] for index in start_index[selected]),
    )


def detect_terrain_conflicts(
    controls: TerrainControlSet,
    *,
    horizontal_threshold_m: float = 2.0,
    vertical_threshold_m: float = 3.0,
    sample_step_m: float = 2.0,
    max_conflicts: int = 1000,
) -> tuple[TerrainConflict, ...]:
    if (
        horizontal_threshold_m <= 0.0
        or vertical_threshold_m <= 0.0
        or max_conflicts <= 0
    ):
        raise ValueError("terrain conflict thresholds must be positive")
    samples, source_ids = sampled_control_points(controls, step_m=sample_step_m)
    if len(samples) < 2:
        return ()
    by_source = {
        source_id: np.flatnonzero(
            np.asarray(source_ids, dtype=object) == source_id
        )
        for source_id in sorted(set(source_ids))
    }
    conflicts: list[TerrainConflict] = []
    for first_source, second_source in combinations(by_source, 2):
        first_indices = by_source[first_source]
        second_indices = by_source[second_source]
        if not len(first_indices) or not len(second_indices):
            continue
        distances, nearest = cKDTree(samples[second_indices, :2]).query(
            samples[first_indices, :2],
            k=1,
            distance_upper_bound=float(horizontal_threshold_m),
        )
        for local_index in np.flatnonzero(np.isfinite(distances)):
            first = int(first_indices[local_index])
            second = int(second_indices[int(nearest[local_index])])
            vertical = abs(float(samples[first, 2] - samples[second, 2]))
            if vertical <= vertical_threshold_m:
                continue
            midpoint = tuple(
                float(value)
                for value in (samples[first, :2] + samples[second, :2]) * 0.5
            )
            conflicts.append(
                TerrainConflict(
                    source_ids=(first_source, second_source),
                    horizontal_distance_m=float(distances[local_index]),
                    vertical_difference_m=vertical,
                    midpoint_xy=midpoint,
                )
            )
            if len(conflicts) >= max_conflicts:
                return tuple(conflicts)
    return tuple(conflicts)
