"""WGS84 horizontal ENU conversion with a separately defined relative Up axis."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import cos, isfinite, radians, sin
from typing import Any, Mapping, Sequence

import numpy as np


WGS84_A_M = 6_378_137.0
WGS84_E2 = 6.6943799901413165e-3


@dataclass(frozen=True)
class EnuOrigin:
    latitude: float
    longitude: float
    ecef_reference_height_m: float


@dataclass(frozen=True)
class LocalEnuPoint:
    source_index: int
    latitude: float
    longitude: float
    east_m: float
    north_m: float
    up_m: float | None
    rel_alt: float | None
    abs_alt: float | None
    selected_height: float | None
    height_source: str


def geodetic_to_ecef(latitude_deg: float, longitude_deg: float, ellipsoid_height_m: float) -> np.ndarray:
    """Convert WGS84 geodetic latitude/longitude/ellipsoid height to ECEF metres."""

    latitude = radians(float(latitude_deg))
    longitude = radians(float(longitude_deg))
    height = float(ellipsoid_height_m)
    if not all(isfinite(value) for value in (latitude, longitude, height)):
        raise ValueError("geodetic coordinates must be finite")
    if not -90.0 <= float(latitude_deg) <= 90.0 or not -180.0 <= float(longitude_deg) <= 180.0:
        raise ValueError("latitude or longitude is outside WGS84 bounds")
    prime_vertical = WGS84_A_M / (1.0 - WGS84_E2 * sin(latitude) ** 2) ** 0.5
    return np.array(
        [
            (prime_vertical + height) * cos(latitude) * cos(longitude),
            (prime_vertical + height) * cos(latitude) * sin(longitude),
            (prime_vertical * (1.0 - WGS84_E2) + height) * sin(latitude),
        ],
        dtype=np.float64,
    )


def _value(sample: Mapping[str, Any] | object, name: str) -> Any:
    return sample.get(name) if isinstance(sample, Mapping) else getattr(sample, name, None)


def _finite(sample: Mapping[str, Any] | object, name: str) -> float | None:
    try:
        value = float(_value(sample, name))
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) else None


def _valid_lat_lon(sample: Mapping[str, Any] | object) -> tuple[float, float] | None:
    latitude = _finite(sample, "latitude")
    longitude = _finite(sample, "longitude")
    if latitude is None or longitude is None or not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        return None
    return latitude, longitude


def _enu_rotation(origin: EnuOrigin) -> np.ndarray:
    latitude = radians(origin.latitude)
    longitude = radians(origin.longitude)
    return np.array(
        [
            [-sin(longitude), cos(longitude), 0.0],
            [-sin(latitude) * cos(longitude), -sin(latitude) * sin(longitude), cos(latitude)],
            [cos(latitude) * cos(longitude), cos(latitude) * sin(longitude), sin(latitude)],
        ],
        dtype=np.float64,
    )


def build_local_enu(
    samples: Sequence[Mapping[str, Any] | object],
    *,
    height_source: str = "auto",
    ecef_reference_height_source: str = "auto",
    ecef_reference_height_m: float | None = None,
) -> tuple[list[LocalEnuPoint], dict[str, Any]]:
    """Build local ENU points without treating relative altitude as ellipsoid height.

    East/North always derive from latitude/longitude at one fixed ECEF reference
    height.  Up is independently relative to the first usable rel_alt or
    abs_alt value, so it is never advertised as an absolute CAD elevation.
    """

    valid = [(index, coordinates) for index, sample in enumerate(samples) if (coordinates := _valid_lat_lon(sample))]
    if not valid:
        raise ValueError("SRT contains no valid latitude/longitude samples")
    first_index, (origin_latitude, origin_longitude) = valid[0]
    first_abs_alt = next((_finite(samples[index], "abs_alt") for index, _ in valid if _finite(samples[index], "abs_alt") is not None), None)

    source = ecef_reference_height_source
    if source == "auto":
        source = "first_abs_alt_unverified" if first_abs_alt is not None else "zero"
    if source == "zero":
        reference_height = 0.0
    elif source == "first_abs_alt_unverified":
        reference_height = 0.0 if first_abs_alt is None else first_abs_alt
    elif source == "verified_ellipsoid_height":
        if ecef_reference_height_m is None or not isfinite(float(ecef_reference_height_m)):
            raise ValueError("verified_ellipsoid_height requires a finite ecef_reference_height_m")
        reference_height = float(ecef_reference_height_m)
    else:
        raise ValueError("ecef_reference_height_source must be auto, zero, first_abs_alt_unverified, or verified_ellipsoid_height")

    rel_values = [_finite(sample, "rel_alt") for sample in samples]
    abs_values = [_finite(sample, "abs_alt") for sample in samples]
    if height_source == "auto":
        selected_source = "rel_alt" if any(value is not None for value in rel_values) else "abs_alt"
    elif height_source in {"rel_alt", "abs_alt"}:
        selected_source = height_source
    else:
        raise ValueError("height_source must be auto, rel_alt, or abs_alt")
    selected_values = rel_values if selected_source == "rel_alt" else abs_values
    first_height = next((value for value in selected_values if value is not None), None)
    up_label = f"{selected_source}_relative" if first_height is not None else "unavailable"

    origin = EnuOrigin(origin_latitude, origin_longitude, reference_height)
    origin_ecef = geodetic_to_ecef(origin.latitude, origin.longitude, origin.ecef_reference_height_m)
    rotation = _enu_rotation(origin)
    points: list[LocalEnuPoint] = []
    for index, (latitude, longitude) in valid:
        ecef = geodetic_to_ecef(latitude, longitude, reference_height)
        east, north, _ = rotation @ (ecef - origin_ecef)
        selected_height = selected_values[index]
        points.append(
            LocalEnuPoint(
                source_index=index,
                latitude=latitude,
                longitude=longitude,
                east_m=float(east),
                north_m=float(north),
                up_m=None if selected_height is None or first_height is None else float(selected_height - first_height),
                rel_alt=rel_values[index],
                abs_alt=abs_values[index],
                selected_height=selected_height,
                height_source=up_label,
            )
        )
    return points, {
        "horizontal_datum": "WGS84 local ENU",
        "enu_origin": asdict(origin),
        "ecef_reference_height_source": source,
        "ecef_reference_height_m": reference_height,
        "up_axis": "ENU up (independent relative height)",
        "up_source": up_label,
        "absolute_elevation_available": source == "verified_ellipsoid_height",
        "origin_source_index": first_index,
    }
