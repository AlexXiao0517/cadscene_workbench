"""CGCS2000 projected-coordinate configuration and candidate recommendation."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from math import hypot, isfinite
import re
from statistics import median
from typing import Callable, Mapping, Sequence

from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info
from pyproj.enums import PJType


_AXIS_MAPPINGS = frozenset(
    {
        "cad_x_easting_cad_y_northing",
        "cad_x_northing_cad_y_easting",
    }
)
_DECIMAL_MERIDIAN_PATTERN = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:°|度)?$"
)
_DEGREE_MINUTE_MERIDIAN_PATTERN = re.compile(
    r"^([+-]?\d{1,3})(?:\s*(?:°|度)\s*|\s+)(\d+(?:\.\d+)?)\s*(?:′|'|分)?$"
)


def parse_central_meridian(value: object | None) -> float | None:
    """Normalize decimal or degree-minute central-meridian input."""

    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("central meridian format is invalid")
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        decimal_match = _DECIMAL_MERIDIAN_PATTERN.fullmatch(text)
        if decimal_match is not None:
            result = float(decimal_match.group(1))
        else:
            degree_minute_match = _DEGREE_MINUTE_MERIDIAN_PATTERN.fullmatch(text)
            if degree_minute_match is None:
                raise ValueError("central meridian format is invalid")
            degree_text, minute_text = degree_minute_match.groups()
            minutes = float(minute_text)
            if not isfinite(minutes) or not 0.0 <= minutes < 60.0:
                raise ValueError("central meridian minutes must be between 0 and 60")
            degrees = abs(float(degree_text))
            sign = -1.0 if degree_text.startswith("-") else 1.0
            result = sign * (degrees + minutes / 60.0)
    if not isfinite(result) or not -180.0 <= result <= 180.0:
        raise ValueError("central meridian must be between -180 and 180")
    return result


class GeoreferenceEnvironmentError(RuntimeError):
    """Raised when the installed PROJ runtime cannot serve required CRS data."""


@dataclass(frozen=True)
class CadGeoreference:
    schema_version: int
    horizontal_datum: str
    projection_family: str
    zone_width_deg: int
    central_meridian_deg: float
    epsg: int | None
    projected_axis_order: str
    cad_axis_mapping: str
    zone_prefix: bool
    linear_unit: str
    source: str
    confirmed: bool
    confidence: float
    validation: Mapping[str, object] = field(default_factory=dict)
    crs_source: str = "epsg"
    latitude_of_origin_deg: float = 0.0
    scale_factor: float = 1.0
    false_easting_m: float = 500_000.0
    false_northing_m: float = 0.0
    ellipsoid: str = "GRS80"

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CadGeoreference":
        try:
            raw_epsg = value.get("epsg")
            config = cls(
                schema_version=int(value["schema_version"]),
                horizontal_datum=str(value["horizontal_datum"]),
                projection_family=str(value["projection_family"]),
                zone_width_deg=int(value["zone_width_deg"]),
                central_meridian_deg=float(value["central_meridian_deg"]),
                epsg=None if raw_epsg is None else int(raw_epsg),
                projected_axis_order=str(value["projected_axis_order"]),
                cad_axis_mapping=str(value["cad_axis_mapping"]),
                zone_prefix=bool(value["zone_prefix"]),
                linear_unit=str(value["linear_unit"]),
                source=str(value["source"]),
                confirmed=bool(value["confirmed"]),
                confidence=float(value["confidence"]),
                validation=dict(value.get("validation") or {}),
                crs_source=str(
                    value.get("crs_source")
                    or ("custom" if raw_epsg is None else "epsg")
                ),
                latitude_of_origin_deg=float(
                    value.get("latitude_of_origin_deg", 0.0)
                ),
                scale_factor=float(value.get("scale_factor", 1.0)),
                false_easting_m=float(value.get("false_easting_m", 500_000.0)),
                false_northing_m=float(value.get("false_northing_m", 0.0)),
                ellipsoid=str(value.get("ellipsoid", "GRS80")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid cad georeference payload: {exc}") from exc
        config._validate()
        return config

    def _validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("cad georeference schema_version must be 1")
        if self.horizontal_datum != "CGCS2000":
            raise ValueError("horizontal_datum must be CGCS2000")
        if self.projection_family != "gauss_kruger":
            raise ValueError("projection_family must be gauss_kruger")
        if self.zone_width_deg not in {3, 6}:
            raise ValueError("zone_width_deg must be 3 or 6")
        if not isfinite(self.central_meridian_deg):
            raise ValueError("central_meridian_deg must be finite")
        if self.projected_axis_order != "easting_northing":
            raise ValueError("projected_axis_order must be easting_northing")
        if self.cad_axis_mapping not in _AXIS_MAPPINGS:
            raise ValueError("unsupported cad_axis_mapping")
        if self.linear_unit != "metre":
            raise ValueError("linear_unit must be metre")
        if not self.source:
            raise ValueError("source must not be empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")

        if self.crs_source == "epsg":
            if self.epsg is None:
                raise ValueError("epsg is required for an EPSG-backed projection")
            crs = _crs_from_epsg(self.epsg)
            if not crs.is_projected or not crs.name.startswith("CGCS2000 /"):
                raise ValueError("epsg must identify a projected CGCS2000 CRS")
            if "Gauss-Kruger" not in crs.name:
                raise ValueError("epsg must identify a CGCS2000 Gauss-Kruger CRS")
            units = {str(axis.unit_name).lower() for axis in crs.axis_info}
            if not units or any(unit not in {"metre", "meter"} for unit in units):
                raise ValueError("epsg projected axes must use metres")
            actual_meridian = _central_meridian(crs)
            if abs(actual_meridian - self.central_meridian_deg) > 1e-9:
                raise ValueError(
                    "central_meridian_deg disagrees with the configured epsg"
                )
            actual_zone_width = 3 if "3-degree" in crs.name else 6
            if actual_zone_width != self.zone_width_deg:
                raise ValueError("zone_width_deg disagrees with the configured epsg")
            actual_zone_prefix = " zone " in f" {crs.name.lower()} "
            if actual_zone_prefix != self.zone_prefix:
                raise ValueError("zone_prefix disagrees with the configured epsg")
        elif self.crs_source == "custom":
            if self.epsg is not None:
                raise ValueError("custom projection must not declare an epsg")
            if self.zone_prefix:
                raise ValueError("custom projection does not support a zone prefix")
            fixed_parameters = (
                ("latitude_of_origin_deg", self.latitude_of_origin_deg, 0.0),
                ("scale_factor", self.scale_factor, 1.0),
                ("false_easting_m", self.false_easting_m, 500_000.0),
                ("false_northing_m", self.false_northing_m, 0.0),
            )
            for name, actual, expected in fixed_parameters:
                if not isfinite(actual) or abs(actual - expected) > 1e-9:
                    raise ValueError(
                        f"custom projection {name} must equal {expected:g}"
                    )
            if self.ellipsoid != "GRS80":
                raise ValueError("custom projection ellipsoid must be GRS80")
            _custom_crs(self.central_meridian_deg)
        else:
            raise ValueError("crs_source must be epsg or custom")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "horizontal_datum": self.horizontal_datum,
            "projection_family": self.projection_family,
            "zone_width_deg": self.zone_width_deg,
            "central_meridian_deg": self.central_meridian_deg,
            "epsg": self.epsg,
            "projected_axis_order": self.projected_axis_order,
            "cad_axis_mapping": self.cad_axis_mapping,
            "zone_prefix": self.zone_prefix,
            "linear_unit": self.linear_unit,
            "source": self.source,
            "confirmed": self.confirmed,
            "confidence": self.confidence,
            "validation": dict(self.validation),
            "crs_source": self.crs_source,
        }
        if self.crs_source == "custom":
            payload.update(
                {
                    "latitude_of_origin_deg": self.latitude_of_origin_deg,
                    "scale_factor": self.scale_factor,
                    "false_easting_m": self.false_easting_m,
                    "false_northing_m": self.false_northing_m,
                    "ellipsoid": self.ellipsoid,
                }
            )
        return payload


@dataclass(frozen=True)
class CrsCandidate:
    epsg: int | None
    crs_name: str
    zone_width_deg: int
    central_meridian_deg: float
    cad_axis_mapping: str
    zone_prefix: bool
    score: float
    evidence: Mapping[str, object]
    confirmed: bool = False
    crs_source: str = "epsg"
    latitude_of_origin_deg: float = 0.0
    scale_factor: float = 1.0
    false_easting_m: float = 500_000.0
    false_northing_m: float = 0.0
    ellipsoid: str = "GRS80"

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "epsg": self.epsg,
            "crs_name": self.crs_name,
            "zone_width_deg": self.zone_width_deg,
            "central_meridian_deg": self.central_meridian_deg,
            "cad_axis_mapping": self.cad_axis_mapping,
            "zone_prefix": self.zone_prefix,
            "score": self.score,
            "evidence": dict(self.evidence),
            "confirmed": False,
            "crs_source": self.crs_source,
        }
        if self.crs_source == "custom":
            payload.update(
                {
                    "latitude_of_origin_deg": self.latitude_of_origin_deg,
                    "scale_factor": self.scale_factor,
                    "false_easting_m": self.false_easting_m,
                    "false_northing_m": self.false_northing_m,
                    "ellipsoid": self.ellipsoid,
                }
            )
        return payload


@lru_cache(maxsize=64)
def _crs_from_epsg(epsg: int) -> CRS:
    try:
        return CRS.from_epsg(int(epsg))
    except Exception as exc:  # pyproj raises different subclasses by PROJ version
        raise GeoreferenceEnvironmentError(
            f"PROJ cannot load EPSG:{int(epsg)}: {exc}"
        ) from exc


@lru_cache(maxsize=64)
def _transformer(epsg: int) -> Transformer:
    try:
        return Transformer.from_crs(
            CRS.from_epsg(4326),
            _crs_from_epsg(epsg),
            always_xy=True,
        )
    except Exception as exc:  # pragma: no cover - environment-specific failure
        raise GeoreferenceEnvironmentError(
            f"PROJ cannot create the EPSG:4326 to EPSG:{epsg} transformer: {exc}"
        ) from exc


@lru_cache(maxsize=64)
def _custom_crs(central_meridian_deg: float) -> CRS:
    meridian = parse_central_meridian(central_meridian_deg)
    if meridian is None:
        raise ValueError("custom central meridian is required")
    try:
        return CRS.from_proj4(
            "+proj=tmerc +lat_0=0 "
            f"+lon_0={meridian:.15g} "
            "+k=1 +x_0=500000 +y_0=0 +ellps=GRS80 +units=m +no_defs +type=crs"
        )
    except Exception as exc:
        raise GeoreferenceEnvironmentError(
            f"PROJ cannot create custom CGCS2000 projection: {exc}"
        ) from exc


@lru_cache(maxsize=64)
def _custom_transformer(central_meridian_deg: float) -> Transformer:
    try:
        return Transformer.from_crs(
            CRS.from_epsg(4326),
            _custom_crs(central_meridian_deg),
            always_xy=True,
        )
    except Exception as exc:
        raise GeoreferenceEnvironmentError(
            f"PROJ cannot create a custom CGCS2000 transformer: {exc}"
        ) from exc


def _central_meridian(crs: CRS) -> float:
    operation = crs.coordinate_operation
    if operation is None:
        raise ValueError(f"{crs.name} has no coordinate operation")
    for parameter in operation.params:
        name = str(parameter.name).lower()
        if name in {
            "longitude of natural origin",
            "longitude of false origin",
            "central meridian",
        }:
            return float(parameter.value)
    raise ValueError(f"{crs.name} does not declare a central meridian")


def _mapped_xy(
    easting: float, northing: float, cad_axis_mapping: str
) -> tuple[float, float]:
    if cad_axis_mapping == "cad_x_easting_cad_y_northing":
        return float(easting), float(northing)
    if cad_axis_mapping == "cad_x_northing_cad_y_easting":
        return float(northing), float(easting)
    raise ValueError("unsupported cad_axis_mapping")


def project_wgs84_to_cad_raw(
    longitude: float,
    latitude: float,
    config: CadGeoreference | Mapping[str, object],
) -> tuple[float, float]:
    resolved = (
        config
        if isinstance(config, CadGeoreference)
        else CadGeoreference.from_dict(config)
    )
    if not resolved.confirmed:
        raise ValueError("cad georeference must be confirmed before projection")
    lon = float(longitude)
    lat = float(latitude)
    if not isfinite(lon) or not isfinite(lat) or not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        raise ValueError("longitude/latitude must be finite WGS84 coordinates")
    transformer = (
        _transformer(resolved.epsg)
        if resolved.crs_source == "epsg" and resolved.epsg is not None
        else _custom_transformer(resolved.central_meridian_deg)
    )
    easting, northing = transformer.transform(lon, lat)
    if not isfinite(easting) or not isfinite(northing):
        raise ValueError("coordinate projection produced non-finite values")
    return _mapped_xy(easting, northing, resolved.cad_axis_mapping)


def cad_raw_to_local_m(
    cad_xy: Sequence[float],
    origin_xy: Sequence[float],
    cad_scale: float,
) -> tuple[float, float]:
    if len(cad_xy) != 2 or len(origin_xy) != 2:
        raise ValueError("cad_xy and origin_xy must each contain two values")
    x, y = (float(value) for value in cad_xy)
    ox, oy = (float(value) for value in origin_xy)
    scale = float(cad_scale)
    if not all(isfinite(value) for value in (x, y, ox, oy, scale)) or scale <= 0.0:
        raise ValueError("CAD coordinates and positive cad_scale must be finite")
    return (x - ox) * scale, (y - oy) * scale


def _validated_samples(
    longitudes: Sequence[float], latitudes: Sequence[float]
) -> tuple[tuple[float, float], ...]:
    if len(longitudes) != len(latitudes) or not longitudes:
        raise ValueError("longitude and latitude samples must have the same non-zero length")
    samples: list[tuple[float, float]] = []
    for longitude, latitude in zip(longitudes, latitudes):
        lon = float(longitude)
        lat = float(latitude)
        if not isfinite(lon) or not isfinite(lat) or not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
            raise ValueError("candidate samples must be finite WGS84 coordinates")
        samples.append((lon, lat))
    return tuple(samples)


def _validated_bbox(cad_bbox_raw: Sequence[float]) -> tuple[float, float, float, float]:
    if len(cad_bbox_raw) != 4:
        raise ValueError("cad_bbox_raw must contain min_x, min_y, max_x, max_y")
    min_x, min_y, max_x, max_y = (float(value) for value in cad_bbox_raw)
    if not all(isfinite(value) for value in (min_x, min_y, max_x, max_y)):
        raise ValueError("cad_bbox_raw values must be finite")
    if max_x <= min_x or max_y <= min_y:
        raise ValueError("cad_bbox_raw must have positive width and height")
    return min_x, min_y, max_x, max_y


def _distance_to_bbox(
    point: tuple[float, float], bbox: tuple[float, float, float, float]
) -> float:
    x, y = point
    min_x, min_y, max_x, max_y = bbox
    dx = max(min_x - x, 0.0, x - max_x)
    dy = max(min_y - y, 0.0, y - max_y)
    return hypot(dx, dy)


def _candidate_score(
    points: Sequence[tuple[float, float]],
    bbox: tuple[float, float, float, float],
) -> tuple[float, dict[str, object]]:
    min_x, min_y, max_x, max_y = bbox
    span = max(max_x - min_x, max_y - min_y, 1.0)
    buffer = max(span * 0.1, 10.0)
    inside = [
        min_x <= x <= max_x and min_y <= y <= max_y for x, y in points
    ]
    buffered = [
        min_x - buffer <= x <= max_x + buffer
        and min_y - buffer <= y <= max_y + buffer
        for x, y in points
    ]
    distances = [_distance_to_bbox(point, bbox) for point in points]
    inside_ratio = sum(inside) / len(points)
    buffered_ratio = sum(buffered) / len(points)
    median_distance = float(median(distances))
    score = 100.0 * inside_ratio + 20.0 * buffered_ratio - min(
        100.0, median_distance / span
    )
    if len(points) <= 80:
        preview_points = points
    else:
        indexes = {
            round(index * (len(points) - 1) / 79) for index in range(80)
        }
        preview_points = [points[index] for index in sorted(indexes)]
    return score, {
        "trajectory_inside_cad_ratio": inside_ratio,
        "trajectory_inside_buffer_ratio": buffered_ratio,
        "median_distance_to_cad_bbox_m": median_distance,
        "cad_bbox_raw": [min_x, min_y, max_x, max_y],
        "trajectory_polyline_raw": [list(point) for point in preview_points],
    }


def recommend_cgcs2000_candidates(
    longitudes: Sequence[float],
    latitudes: Sequence[float],
    cad_bbox_raw: Sequence[float],
    *,
    limit: int = 6,
    central_meridian_deg: float | None = None,
    progress_callback: Callable[[str, str, float | None], None] | None = None,
) -> tuple[CrsCandidate, ...]:
    def report(stage: str, message: str, fraction: float | None) -> None:
        if progress_callback is not None:
            progress_callback(stage, message, fraction)

    report("validating_inputs", "正在校验 SRT 轨迹与 CAD 坐标范围", None)
    samples = _validated_samples(longitudes, latitudes)
    bbox = _validated_bbox(cad_bbox_raw)
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    requested_meridian = None
    if central_meridian_deg is not None:
        requested_meridian = float(central_meridian_deg)
        if not isfinite(requested_meridian) or not -180.0 <= requested_meridian <= 180.0:
            raise ValueError(
                "central_meridian_deg must be finite and between -180 and 180"
            )
    lon_values = [item[0] for item in samples]
    lat_values = [item[1] for item in samples]
    area = AreaOfInterest(
        min(lon_values) - 0.01,
        min(lat_values) - 0.01,
        max(lon_values) + 0.01,
        max(lat_values) + 0.01,
    )
    report("enumerating_crs", "正在枚举 CGCS2000 高斯-克吕格候选", None)
    try:
        infos = query_crs_info(
            auth_name="EPSG",
            pj_types=[PJType.PROJECTED_CRS],
            area_of_interest=area,
            contains=False,
        )
    except Exception as exc:  # pragma: no cover - environment-specific failure
        raise GeoreferenceEnvironmentError(
            f"PROJ cannot query CGCS2000 candidates: {exc}"
        ) from exc

    descriptors: list[tuple[str, int | None, float, int, bool, str]] = []
    for info in infos:
        if not info.name.startswith("CGCS2000 /") or "Gauss-Kruger" not in info.name:
            continue
        epsg = int(info.code)
        crs = _crs_from_epsg(epsg)
        central_meridian = _central_meridian(crs)
        if (
            requested_meridian is not None
            and abs(central_meridian - requested_meridian) > 1e-9
        ):
            continue
        zone_width = 3 if "3-degree" in info.name else 6
        zone_prefix = " zone " in f" {info.name.lower()} "
        descriptors.append(
            (info.name, epsg, central_meridian, zone_width, zone_prefix, "epsg")
        )
    if requested_meridian is not None and not descriptors:
        descriptors.append(
            (
                "CGCS2000 / custom Gauss-Kruger",
                None,
                requested_meridian,
                3,
                False,
                "custom",
            )
        )

    candidates: list[CrsCandidate] = []
    total_variants = len(descriptors) * len(_AXIS_MAPPINGS)
    processed_variants = 0
    for crs_name, epsg, central_meridian, zone_width, zone_prefix, crs_source in descriptors:
        transformer = (
            _transformer(epsg)
            if crs_source == "epsg" and epsg is not None
            else _custom_transformer(central_meridian)
        )
        projected = [transformer.transform(lon, lat) for lon, lat in samples]
        if any(not isfinite(east) or not isfinite(north) for east, north in projected):
            processed_variants += len(_AXIS_MAPPINGS)
            if total_variants:
                report(
                    "scoring_candidates",
                    f"正在评分坐标候选 {processed_variants}/{total_variants}",
                    processed_variants / total_variants,
                )
            continue
        for mapping in sorted(_AXIS_MAPPINGS):
            points = [_mapped_xy(east, north, mapping) for east, north in projected]
            score, evidence = _candidate_score(points, bbox)
            evidence.update(
                {
                    "axis_mapping": mapping,
                    "sample_count": len(points),
                    "zone_prefix": zone_prefix,
                }
            )
            candidates.append(
                CrsCandidate(
                    epsg=epsg,
                    crs_name=crs_name,
                    zone_width_deg=zone_width,
                    central_meridian_deg=central_meridian,
                    cad_axis_mapping=mapping,
                    zone_prefix=zone_prefix,
                    score=float(score),
                    evidence=evidence,
                    crs_source=crs_source,
                )
            )
            processed_variants += 1
            report(
                "scoring_candidates",
                f"正在评分坐标候选 {processed_variants}/{total_variants}",
                processed_variants / total_variants,
            )
    report("building_previews", "正在整理候选证据与轨迹预览", None)
    candidates.sort(
        key=lambda item: (
            -item.score,
            abs(item.central_meridian_deg - median(lon_values)),
            item.zone_prefix,
            item.zone_width_deg,
            -1 if item.epsg is None else item.epsg,
            item.cad_axis_mapping,
        )
    )
    result = tuple(candidates[: int(limit)])
    report("complete", "坐标系候选生成完成", 1.0)
    return result
