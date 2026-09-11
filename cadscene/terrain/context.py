from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from .tpkg import TerrainControlSet, route_coverage
from .drape import detect_terrain_conflicts


@dataclass(frozen=True)
class TerrainContext:
    mode: str
    coverage_fraction: float
    covered_route_points: int
    total_route_points: int
    max_control_distance_m: float
    height_range_m: tuple[float, float] | None
    source_fingerprints: tuple[str, ...]
    controls_fingerprint: str | None
    cad_fingerprint: str
    georeference_fingerprint: str
    reference_ground_m: float | None = None
    cad_fallback_ground_m: float | None = None
    camera_height_datum_valid: bool = False
    camera_height_datum_source: str = "unresolved"
    uncovered_intervals: tuple[Mapping[str, object], ...] = ()
    conflicts: tuple[Mapping[str, object], ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "cadscene_terrain_context_v1",
            "terrain_mode": self.mode,
            "terrain_coverage": self.coverage_fraction,
            "covered_route_points": self.covered_route_points,
            "total_route_points": self.total_route_points,
            "max_control_distance_m": self.max_control_distance_m,
            "terrain_height_range_m": None if self.height_range_m is None else list(self.height_range_m),
            "terrain_source_fingerprints": list(self.source_fingerprints),
            "terrain_controls_fingerprint": self.controls_fingerprint,
            "cad_fingerprint": self.cad_fingerprint,
            "georeference_fingerprint": self.georeference_fingerprint,
            "terrain_reference_ground_m": self.reference_ground_m,
            "cad_fallback_ground_m": self.cad_fallback_ground_m,
            "camera_height_datum_valid": self.camera_height_datum_valid,
            "camera_height_datum_source": self.camera_height_datum_source,
            "terrain_uncovered_intervals": [
                dict(item) for item in self.uncovered_intervals
            ],
            "terrain_conflicts": [dict(item) for item in self.conflicts],
            "terrain_warnings": list(self.warnings),
        }


def build_terrain_context(
    controls: TerrainControlSet | None,
    route_xy: np.ndarray,
    *,
    cad_fingerprint: str,
    georeference_fingerprint: str,
    warnings: Sequence[str] = (),
    route_time_sec: Sequence[float] | None = None,
    max_distance_m: float = 160.0,
) -> TerrainContext:
    route = np.asarray(route_xy, dtype=np.float64).reshape(-1, 2)
    times = (
        tuple(float(value) for value in route_time_sec)
        if route_time_sec is not None
        else tuple(float(index) for index in range(len(route)))
    )
    if len(times) != len(route) or not np.isfinite(np.asarray(times)).all():
        raise ValueError("terrain route times must match route points and be finite")
    if controls is None:
        return TerrainContext(
            mode="relative",
            coverage_fraction=0.0,
            covered_route_points=0,
            total_route_points=len(route),
            max_control_distance_m=max_distance_m,
            height_range_m=None,
            source_fingerprints=(),
            controls_fingerprint=None,
            cad_fingerprint=str(cad_fingerprint),
            georeference_fingerprint=str(georeference_fingerprint),
            warnings=tuple(str(item) for item in warnings),
        )
    coverage = route_coverage(route, controls, max_distance_m=max_distance_m)
    uncovered = _uncovered_intervals(coverage.distances_m, times, max_distance_m)
    conflicts = tuple(item.to_dict() for item in detect_terrain_conflicts(controls))
    context_warnings = [str(item) for item in warnings]
    if uncovered:
        context_warnings.append(
            f"高程未覆盖 {len(uncovered)} 个连续轨迹区间，覆盖率 {coverage.fraction:.1%}"
        )
    if conflicts:
        context_warnings.append(
            f"检测到 {len(conflicts)} 处跨文件高程冲突，请在工作台检查"
        )
    z_values = np.concatenate(
        (controls.points_xyz[:, 2], controls.segment_starts_xyz[:, 2], controls.segment_ends_xyz[:, 2])
    )
    height_range = (float(np.min(z_values)), float(np.max(z_values))) if len(z_values) else None
    return TerrainContext(
        mode=coverage.mode,
        coverage_fraction=coverage.fraction,
        covered_route_points=coverage.covered_count,
        total_route_points=coverage.total_count,
        max_control_distance_m=max_distance_m,
        height_range_m=height_range,
        source_fingerprints=tuple(sorted(source.source_id for source in controls.sources)),
        controls_fingerprint=controls.fingerprint,
        cad_fingerprint=str(cad_fingerprint),
        georeference_fingerprint=str(georeference_fingerprint),
        uncovered_intervals=uncovered,
        conflicts=conflicts,
        warnings=tuple(context_warnings),
    )


def _uncovered_intervals(
    distances_m: np.ndarray,
    route_time_sec: Sequence[float],
    max_distance_m: float,
) -> tuple[Mapping[str, object], ...]:
    uncovered = np.asarray(distances_m, dtype=np.float64) > float(max_distance_m)
    intervals: list[Mapping[str, object]] = []
    start: int | None = None
    for index, missing in enumerate((*uncovered.tolist(), False)):
        if missing and start is None:
            start = index
        elif not missing and start is not None:
            end = index - 1
            intervals.append(
                {
                    "start_index": start,
                    "end_index": end,
                    "start_time_sec": float(route_time_sec[start]),
                    "end_time_sec": float(route_time_sec[end]),
                }
            )
            start = None
    return tuple(intervals)


def write_terrain_context(
    directory: str | Path,
    context: TerrainContext,
    controls: TerrainControlSet | None,
) -> tuple[Path, Path]:
    output = Path(directory)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "terrain_context.json"
    controls_path = output / "terrain_controls.npz"
    json_path.write_text(
        json.dumps(context.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if controls is None:
        np.savez_compressed(
            controls_path,
            points_xyz=np.empty((0, 3)),
            segment_starts_xyz=np.empty((0, 3)),
            segment_ends_xyz=np.empty((0, 3)),
        )
    else:
        np.savez_compressed(
            controls_path,
            points_xyz=controls.points_xyz,
            point_colors_bgr=controls.point_colors_bgr,
            point_labels=np.asarray(controls.point_labels, dtype=np.str_),
            point_source_ids=np.asarray(controls.point_source_ids, dtype=np.str_),
            segment_starts_xyz=controls.segment_starts_xyz,
            segment_ends_xyz=controls.segment_ends_xyz,
            segment_colors_bgr=controls.segment_colors_bgr,
            segment_widths=controls.segment_widths,
            segment_source_ids=np.asarray(controls.segment_source_ids, dtype=np.str_),
        )
    return json_path, controls_path
