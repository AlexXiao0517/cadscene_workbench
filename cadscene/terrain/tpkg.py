from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
import struct
from typing import Callable, Mapping, Sequence

import numpy as np
from scipy.spatial import cKDTree


_ENVELOPE_BYTES = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}


class TerrainValidationError(ValueError):
    """A terrain package cannot provide trustworthy three-dimensional controls."""


@dataclass(frozen=True)
class TerrainSourceSummary:
    source_id: str
    path: str
    feature_count: int
    vertex_count: int
    geometry_types: Mapping[str, int]
    layers: Mapping[str, int]
    bbox_lon_lat: tuple[float, float, float, float]
    z_range_m: tuple[float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "feature_count": self.feature_count,
            "vertex_count": self.vertex_count,
            "geometry_types": dict(self.geometry_types),
            "layers": dict(self.layers),
            "bbox_lon_lat": list(self.bbox_lon_lat),
            "z_range_m": list(self.z_range_m),
        }


@dataclass(frozen=True)
class TerrainControlSet:
    points_xyz: np.ndarray
    point_source_ids: tuple[str, ...]
    point_labels: tuple[str, ...]
    point_colors_bgr: np.ndarray
    segment_starts_xyz: np.ndarray
    segment_ends_xyz: np.ndarray
    segment_source_ids: tuple[str, ...]
    segment_colors_bgr: np.ndarray
    segment_widths: np.ndarray
    sources: tuple[TerrainSourceSummary, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        points = np.asarray(self.points_xyz, dtype=np.float64).reshape(-1, 3)
        starts = np.asarray(self.segment_starts_xyz, dtype=np.float64).reshape(-1, 3)
        ends = np.asarray(self.segment_ends_xyz, dtype=np.float64).reshape(-1, 3)
        point_colors = np.asarray(self.point_colors_bgr, dtype=np.uint8).reshape(-1, 3)
        segment_colors = np.asarray(self.segment_colors_bgr, dtype=np.uint8).reshape(-1, 3)
        widths = np.asarray(self.segment_widths, dtype=np.int32).reshape(-1)
        if len(points) != len(self.point_source_ids) or len(points) != len(self.point_labels):
            raise ValueError("terrain point metadata must match point count")
        if len(point_colors) != len(points):
            raise ValueError("terrain point colours must match point count")
        if len(starts) != len(ends) or len(starts) != len(self.segment_source_ids):
            raise ValueError("terrain segment metadata must match segment count")
        if len(segment_colors) != len(starts) or len(widths) != len(starts):
            raise ValueError("terrain segment styles must match segment count")
        if not all(np.isfinite(value).all() for value in (points, starts, ends)):
            raise ValueError("terrain controls must be finite")
        if len(self.fingerprint) != 64:
            raise ValueError("terrain fingerprint must be a SHA-256 digest")
        object.__setattr__(self, "points_xyz", points)
        object.__setattr__(self, "point_colors_bgr", point_colors)
        object.__setattr__(self, "segment_starts_xyz", starts)
        object.__setattr__(self, "segment_ends_xyz", ends)
        object.__setattr__(self, "segment_colors_bgr", segment_colors)
        object.__setattr__(self, "segment_widths", widths)


@dataclass(frozen=True)
class TerrainCoverage:
    covered_count: int
    total_count: int
    fraction: float
    max_distance_m: float
    mode: str
    distances_m: np.ndarray

    def to_dict(self) -> dict[str, object]:
        finite = self.distances_m[np.isfinite(self.distances_m)]
        return {
            "covered_count": self.covered_count,
            "total_count": self.total_count,
            "fraction": self.fraction,
            "max_distance_m": self.max_distance_m,
            "mode": self.mode,
            "distance_m": {
                "median": float(np.median(finite)) if len(finite) else None,
                "p95": float(np.percentile(finite, 95)) if len(finite) else None,
                "max": float(np.max(finite)) if len(finite) else None,
            },
        }


@dataclass(frozen=True)
class _DecodedGeometry:
    name: str
    parts: tuple[tuple[tuple[float, float, float], ...], ...]

    @property
    def points(self) -> tuple[tuple[float, float, float], ...]:
        return tuple(point for part in self.parts for point in part)


@dataclass(frozen=True)
class _Feature:
    layer: str
    name: str
    style: Mapping[str, object]
    geometry: _DecodedGeometry


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_point(
    blob: bytes, offset: int, byte_order: str, *, dimensions: int
) -> tuple[tuple[float, float, float], int]:
    size = dimensions * 8
    if offset + size > len(blob):
        raise TerrainValidationError("TPKG geometry is truncated")
    values = struct.unpack_from(byte_order + "d" * dimensions, blob, offset)
    if dimensions < 3:
        raise TerrainValidationError("TPKG geometry has no Z dimension")
    point = (float(values[0]), float(values[1]), float(values[2]))
    if not all(math.isfinite(value) for value in point):
        raise TerrainValidationError("TPKG geometry contains non-finite XYZ")
    return point, offset + size


def _decode_geometry(blob: bytes) -> _DecodedGeometry:
    if len(blob) < 13 or blob[:2] != b"GP":
        raise TerrainValidationError("TPKG geometry has no GeoPackage header")
    envelope_type = (blob[3] >> 1) & 0x07
    if envelope_type not in _ENVELOPE_BYTES:
        raise TerrainValidationError("TPKG geometry has an unsupported envelope")
    offset = 8 + _ENVELOPE_BYTES[envelope_type]
    if offset + 5 > len(blob):
        raise TerrainValidationError("TPKG geometry header is truncated")
    byte_order = "<" if blob[offset] == 1 else ">"
    geometry_type = struct.unpack_from(byte_order + "I", blob, offset + 1)[0]
    if geometry_type & 0x80000000:
        base_type = geometry_type & 0x000000FF
        dimensions = 3
    elif 1000 <= geometry_type < 2000:
        base_type = geometry_type - 1000
        dimensions = 3
    else:
        base_type = geometry_type & 0x000000FF
        dimensions = 2
    if dimensions != 3:
        raise TerrainValidationError(
            f"TPKG geometry type {geometry_type} has no Z dimension"
        )
    cursor = offset + 5
    if base_type == 1:
        point, cursor = _read_point(blob, cursor, byte_order, dimensions=dimensions)
        return _DecodedGeometry("PointZ", ((point,),))
    if base_type == 2:
        if cursor + 4 > len(blob):
            raise TerrainValidationError("TPKG LineStringZ is truncated")
        count = struct.unpack_from(byte_order + "I", blob, cursor)[0]
        cursor += 4
        part = []
        for _ in range(count):
            point, cursor = _read_point(blob, cursor, byte_order, dimensions=dimensions)
            part.append(point)
        return _DecodedGeometry("LineStringZ", (tuple(part),))
    if base_type == 3:
        if cursor + 4 > len(blob):
            raise TerrainValidationError("TPKG PolygonZ is truncated")
        ring_count = struct.unpack_from(byte_order + "I", blob, cursor)[0]
        cursor += 4
        parts = []
        for _ in range(ring_count):
            if cursor + 4 > len(blob):
                raise TerrainValidationError("TPKG PolygonZ ring is truncated")
            point_count = struct.unpack_from(byte_order + "I", blob, cursor)[0]
            cursor += 4
            ring = []
            for _ in range(point_count):
                point, cursor = _read_point(blob, cursor, byte_order, dimensions=dimensions)
                ring.append(point)
            parts.append(tuple(ring))
        return _DecodedGeometry("PolygonZ", tuple(parts))
    raise TerrainValidationError(
        f"unsupported TPKG geometry type {geometry_type}"
    )


def _features(path: Path) -> tuple[_Feature, ...]:
    try:
        connection = sqlite3.connect(
            "file:" + path.resolve().as_posix() + "?mode=ro", uri=True
        )
    except sqlite3.Error as exc:
        raise TerrainValidationError(f"cannot open TPKG: {exc}") from exc
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master where type='table'"
            )
        }
        if "tx_feature_1" not in tables:
            raise TerrainValidationError("TPKG is missing tx_feature_1")
        has_attributes = "tx_feature_1_attribute" in tables
        query = (
            "select f.layer, f.geom, coalesce(a.name, ''), "
            "a.geometry_style, coalesce(a.visible, 1) "
            "from tx_feature_1 f left join tx_feature_1_attribute a on a.fid=f.fid"
            if has_attributes
            else "select f.layer, f.geom, '', null, 1 from tx_feature_1 f"
        )
        output: list[_Feature] = []
        for layer, blob, name, style_text, visible in connection.execute(query):
            if not int(visible):
                continue
            try:
                style = json.loads(style_text) if style_text else {}
            except (TypeError, json.JSONDecodeError) as exc:
                raise TerrainValidationError("TPKG geometry style is invalid JSON") from exc
            if not isinstance(style, Mapping):
                raise TerrainValidationError("TPKG geometry style must be an object")
            output.append(
                _Feature(
                    layer=str(layer or ""),
                    name=str(name or ""),
                    style=dict(style),
                    geometry=_decode_geometry(bytes(blob)),
                )
            )
        if not output:
            raise TerrainValidationError("TPKG contains no visible 3D features")
        return tuple(output)
    except sqlite3.Error as exc:
        raise TerrainValidationError(f"invalid TPKG database: {exc}") from exc
    finally:
        connection.close()


def inspect_tpkg(path: str | Path) -> TerrainSourceSummary:
    source = Path(path)
    if not source.is_file():
        raise TerrainValidationError(f"TPKG file is unavailable: {source}")
    if source.suffix.lower() != ".tpkg":
        raise TerrainValidationError("terrain source must use the .tpkg extension")
    features = _features(source)
    geometry_types = Counter(feature.geometry.name for feature in features)
    layers = Counter(feature.layer for feature in features)
    points = [point for feature in features for point in feature.geometry.points]
    xyz = np.asarray(points, dtype=np.float64)
    return TerrainSourceSummary(
        source_id=_file_sha256(source),
        path=str(source),
        feature_count=len(features),
        vertex_count=len(points),
        geometry_types=dict(geometry_types),
        layers=dict(layers),
        bbox_lon_lat=(
            float(xyz[:, 0].min()),
            float(xyz[:, 1].min()),
            float(xyz[:, 0].max()),
            float(xyz[:, 1].max()),
        ),
        z_range_m=(float(xyz[:, 2].min()), float(xyz[:, 2].max())),
    )


def _rgb_float_to_bgr(
    value: object, default: tuple[int, int, int]
) -> tuple[int, int, int]:
    if not isinstance(value, list) or len(value) < 3:
        return default
    try:
        rgb = [int(round(float(channel) * 255.0)) for channel in value[:3]]
    except (TypeError, ValueError):
        return default
    rgb = [min(255, max(0, channel)) for channel in rgb]
    return rgb[2], rgb[1], rgb[0]


def _line_style(style: Mapping[str, object]) -> tuple[tuple[int, int, int], int]:
    polyline = style.get("polylineStyle")
    symbol = polyline.get("lineSymbol") if isinstance(polyline, Mapping) else None
    material = symbol.get("material") if isinstance(symbol, Mapping) else None
    color = material.get("color") if isinstance(material, Mapping) else None
    bgr = _rgb_float_to_bgr(color, (0, 255, 255))
    try:
        width = int(round(float(symbol.get("width", 3.0)))) if isinstance(symbol, Mapping) else 3
    except (TypeError, ValueError):
        width = 3
    return bgr, min(12, max(1, width))


def _point_style(style: Mapping[str, object]) -> tuple[int, int, int]:
    point = style.get("pointStyle")
    billboard = point.get("billboardSymbol") if isinstance(point, Mapping) else None
    color = billboard.get("color") if isinstance(billboard, Mapping) else None
    return _rgb_float_to_bgr(color, (255, 218, 115))


def load_tpkg(
    path: str | Path,
    *,
    project_lon_lat: Callable[[float, float], tuple[float, float]],
) -> TerrainControlSet:
    source = Path(path)
    summary = inspect_tpkg(source)
    features = _features(source)
    points: list[tuple[float, float, float]] = []
    point_source_ids: list[str] = []
    point_labels: list[str] = []
    point_colors: list[tuple[int, int, int]] = []
    starts: list[tuple[float, float, float]] = []
    ends: list[tuple[float, float, float]] = []
    segment_source_ids: list[str] = []
    segment_colors: list[tuple[int, int, int]] = []
    segment_widths: list[int] = []

    def transform(point: tuple[float, float, float]) -> tuple[float, float, float]:
        x_m, y_m = project_lon_lat(point[0], point[1])
        result = (float(x_m), float(y_m), float(point[2]))
        if not all(math.isfinite(value) for value in result):
            raise TerrainValidationError("TPKG coordinate projection produced non-finite XYZ")
        return result

    for feature in features:
        transformed_parts = [tuple(transform(point) for point in part) for part in feature.geometry.parts]
        if feature.geometry.name == "PointZ":
            for point in transformed_parts[0]:
                points.append(point)
                point_source_ids.append(summary.source_id)
                point_labels.append(feature.name)
                point_colors.append(_point_style(feature.style))
            continue
        color, width = _line_style(feature.style)
        for part in transformed_parts:
            for first, second in zip(part, part[1:]):
                starts.append(first)
                ends.append(second)
                segment_source_ids.append(summary.source_id)
                segment_colors.append(color)
                segment_widths.append(width)

    return TerrainControlSet(
        points_xyz=np.asarray(points, dtype=np.float64).reshape(-1, 3),
        point_source_ids=tuple(point_source_ids),
        point_labels=tuple(point_labels),
        point_colors_bgr=np.asarray(point_colors, dtype=np.uint8).reshape(-1, 3),
        segment_starts_xyz=np.asarray(starts, dtype=np.float64).reshape(-1, 3),
        segment_ends_xyz=np.asarray(ends, dtype=np.float64).reshape(-1, 3),
        segment_source_ids=tuple(segment_source_ids),
        segment_colors_bgr=np.asarray(segment_colors, dtype=np.uint8).reshape(-1, 3),
        segment_widths=np.asarray(segment_widths, dtype=np.int32),
        sources=(summary,),
        fingerprint=sha256(summary.source_id.encode("ascii")).hexdigest(),
    )


def _stack(items: Sequence[np.ndarray], columns: int, dtype: np.dtype) -> np.ndarray:
    nonempty = [np.asarray(item, dtype=dtype).reshape(-1, columns) for item in items if len(item)]
    return np.vstack(nonempty) if nonempty else np.empty((0, columns), dtype=dtype)


def merge_terrain_controls(items: Sequence[TerrainControlSet]) -> TerrainControlSet:
    if not items:
        raise ValueError("at least one terrain control set is required")
    ordered = tuple(sorted(items, key=lambda item: tuple(source.source_id for source in item.sources)))
    sources_by_id = {
        source.source_id: source
        for item in ordered
        for source in item.sources
    }
    sources = tuple(sources_by_id[key] for key in sorted(sources_by_id))
    fingerprint = sha256(
        "\n".join(source.source_id for source in sources).encode("ascii")
    ).hexdigest()
    return TerrainControlSet(
        points_xyz=_stack([item.points_xyz for item in ordered], 3, np.float64),
        point_source_ids=tuple(value for item in ordered for value in item.point_source_ids),
        point_labels=tuple(value for item in ordered for value in item.point_labels),
        point_colors_bgr=_stack([item.point_colors_bgr for item in ordered], 3, np.uint8),
        segment_starts_xyz=_stack([item.segment_starts_xyz for item in ordered], 3, np.float64),
        segment_ends_xyz=_stack([item.segment_ends_xyz for item in ordered], 3, np.float64),
        segment_source_ids=tuple(value for item in ordered for value in item.segment_source_ids),
        segment_colors_bgr=_stack([item.segment_colors_bgr for item in ordered], 3, np.uint8),
        segment_widths=np.concatenate([item.segment_widths for item in ordered]).astype(np.int32),
        sources=sources,
        fingerprint=fingerprint,
    )


def sampled_control_points(
    controls: TerrainControlSet, *, step_m: float = 2.0
) -> tuple[np.ndarray, tuple[str, ...]]:
    if not math.isfinite(step_m) or step_m <= 0.0:
        raise ValueError("terrain control sample step must be positive")
    samples = [np.asarray(controls.points_xyz, dtype=np.float64)]
    source_ids = list(controls.point_source_ids)
    for start, end, source_id in zip(
        controls.segment_starts_xyz,
        controls.segment_ends_xyz,
        controls.segment_source_ids,
    ):
        horizontal_length = float(np.linalg.norm(end[:2] - start[:2]))
        divisions = max(1, int(np.ceil(horizontal_length / step_m)))
        factors = np.linspace(0.0, 1.0, divisions + 1, dtype=np.float64)[:, None]
        samples.append(start[None, :] + factors * (end - start)[None, :])
        source_ids.extend([source_id] * (divisions + 1))
    available = [sample for sample in samples if len(sample)]
    if not available:
        return np.empty((0, 3), dtype=np.float64), ()
    return np.vstack(available), tuple(source_ids)


def route_coverage(
    route_xy: np.ndarray,
    controls: TerrainControlSet,
    *,
    max_distance_m: float = 160.0,
    effective_fraction: float = 0.95,
) -> TerrainCoverage:
    route = np.asarray(route_xy, dtype=np.float64).reshape(-1, 2)
    if not len(route) or not np.isfinite(route).all():
        raise ValueError("terrain coverage route must contain finite XY points")
    if not math.isfinite(max_distance_m) or max_distance_m <= 0.0:
        raise ValueError("terrain coverage distance must be positive")
    samples, _ = sampled_control_points(controls)
    if not len(samples):
        distances = np.full(len(route), np.inf, dtype=np.float64)
    else:
        distances = np.asarray(cKDTree(samples[:, :2]).query(route, k=1)[0], dtype=np.float64)
    covered = distances <= max_distance_m
    covered_count = int(np.count_nonzero(covered))
    fraction = covered_count / len(route)
    mode = "terrain" if fraction >= effective_fraction else "partial" if covered_count else "relative"
    return TerrainCoverage(
        covered_count=covered_count,
        total_count=len(route),
        fraction=float(fraction),
        max_distance_m=float(max_distance_m),
        mode=mode,
        distances_m=distances,
    )
