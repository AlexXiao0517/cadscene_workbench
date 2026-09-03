"""SRT-constrained camera positions and visual-only attitude recovery."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import hypot, isfinite
from typing import Mapping, Sequence

from cadscene.srt.full_pose import _frame_timestamps
from cadscene.srt.georeference import (
    CadGeoreference,
    cad_raw_to_local_m,
    project_wgs84_to_cad_raw,
)
from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import sample_srt_at_frames


WORKFLOW_NAME = "srt_fixed_track_visual_pose"


@dataclass(frozen=True)
class FixedTrackVisualPoseConfig:
    clip_id: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    georeference: CadGeoreference
    cad_origin_xy: tuple[float, float]
    cad_scale: float
    horizontal_fov_deg: float
    route_offset_xyz_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max_interpolation_gap_sec: float = 1.5
    minimum_position_coverage: float = 0.8
    keyframe_interval_sec: float = 0.5
    max_features: int = 2000
    min_pair_matches: int = 24
    max_orientation_interpolation_gap_sec: float = 2.0
    max_horizontal_speed_mps: float = 100.0

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("clip_id must not be empty")
        if (
            isinstance(self.source_start_pts, bool)
            or isinstance(self.source_end_pts_exclusive, bool)
            or self.source_end_pts_exclusive <= self.source_start_pts
        ):
            raise ValueError("source PTS interval must be a non-empty half-open interval")
        if self.source_time_base <= 0:
            raise ValueError("source_time_base must be positive")
        if len(self.cad_origin_xy) != 2:
            raise ValueError("cad_origin_xy must contain two values")
        if len(self.route_offset_xyz_m) != 3:
            raise ValueError("route_offset_xyz_m must contain three values")
        numeric = (
            *self.cad_origin_xy,
            *self.route_offset_xyz_m,
            self.cad_scale,
            self.horizontal_fov_deg,
            self.max_interpolation_gap_sec,
            self.minimum_position_coverage,
            self.keyframe_interval_sec,
            self.max_orientation_interpolation_gap_sec,
            self.max_horizontal_speed_mps,
        )
        if not all(isfinite(float(value)) for value in numeric):
            raise ValueError("fixed-track numeric configuration must be finite")
        if self.cad_scale <= 0.0:
            raise ValueError("cad_scale must be positive")
        if not 1.0 < self.horizontal_fov_deg < 179.0:
            raise ValueError("horizontal_fov_deg must be inside (1, 179)")
        if self.max_interpolation_gap_sec <= 0.0:
            raise ValueError("max_interpolation_gap_sec must be positive")
        if not 0.0 < self.minimum_position_coverage <= 1.0:
            raise ValueError("minimum_position_coverage must be inside (0, 1]")
        if self.keyframe_interval_sec <= 0.0:
            raise ValueError("keyframe_interval_sec must be positive")
        if isinstance(self.max_features, bool) or self.max_features < 64:
            raise ValueError("max_features must be at least 64")
        if isinstance(self.min_pair_matches, bool) or self.min_pair_matches < 8:
            raise ValueError("min_pair_matches must be at least 8")
        if self.max_orientation_interpolation_gap_sec <= 0.0:
            raise ValueError("max_orientation_interpolation_gap_sec must be positive")
        if self.max_horizontal_speed_mps <= 0.0:
            raise ValueError("max_horizontal_speed_mps must be positive")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FixedTrackVisualPoseConfig":
        time_base = value.get("source_time_base")
        georeference = value.get("georeference")
        origin = value.get("cad_origin_xy")
        offset = value.get("route_offset_xyz_m", (0.0, 0.0, 0.0))
        if not isinstance(time_base, Mapping):
            raise ValueError("source_time_base must be an object")
        if not isinstance(georeference, Mapping):
            raise ValueError("georeference must be an object")
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            raise ValueError("cad_origin_xy must contain two values")
        if not isinstance(offset, (list, tuple)) or len(offset) != 3:
            raise ValueError("route_offset_xyz_m must contain three values")
        return cls(
            clip_id=str(value["clip_id"]),
            source_start_pts=int(value["source_start_pts"]),
            source_end_pts_exclusive=int(value["source_end_pts_exclusive"]),
            source_time_base=Fraction(
                int(time_base["numerator"]), int(time_base["denominator"])
            ),
            georeference=CadGeoreference.from_dict(georeference),
            cad_origin_xy=(float(origin[0]), float(origin[1])),
            cad_scale=float(value["cad_scale"]),
            horizontal_fov_deg=float(value["horizontal_fov_deg"]),
            route_offset_xyz_m=(
                float(offset[0]),
                float(offset[1]),
                float(offset[2]),
            ),
            max_interpolation_gap_sec=float(
                value.get("max_interpolation_gap_sec", 1.5)
            ),
            minimum_position_coverage=float(
                value.get("minimum_position_coverage", 0.8)
            ),
            keyframe_interval_sec=float(value.get("keyframe_interval_sec", 0.5)),
            max_features=int(value.get("max_features", 2000)),
            min_pair_matches=int(value.get("min_pair_matches", 24)),
            max_orientation_interpolation_gap_sec=float(
                value.get("max_orientation_interpolation_gap_sec", 2.0)
            ),
            max_horizontal_speed_mps=float(
                value.get("max_horizontal_speed_mps", 100.0)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_start_pts": self.source_start_pts,
            "source_end_pts_exclusive": self.source_end_pts_exclusive,
            "source_time_base": {
                "numerator": self.source_time_base.numerator,
                "denominator": self.source_time_base.denominator,
            },
            "georeference": self.georeference.to_dict(),
            "cad_origin_xy": list(self.cad_origin_xy),
            "cad_scale": self.cad_scale,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "route_offset_xyz_m": list(self.route_offset_xyz_m),
            "max_interpolation_gap_sec": self.max_interpolation_gap_sec,
            "minimum_position_coverage": self.minimum_position_coverage,
            "keyframe_interval_sec": self.keyframe_interval_sec,
            "max_features": self.max_features,
            "min_pair_matches": self.min_pair_matches,
            "max_orientation_interpolation_gap_sec": (
                self.max_orientation_interpolation_gap_sec
            ),
            "max_horizontal_speed_mps": self.max_horizontal_speed_mps,
        }


@dataclass(frozen=True)
class FixedTrackPosition:
    frame_index: int
    source_pts: int
    pts_time_sec: float
    canonical_center: tuple[float, float, float]
    center: tuple[float, float, float]
    latitude: float
    longitude: float
    rel_alt: float
    abs_alt: float | None
    projected_easting: float
    projected_northing: float
    cad_raw_x: float
    cad_raw_y: float
    interpolated: bool
    source_entry_before: int | None
    source_entry_after: int | None
    height_source: str = "rel_alt"


def build_fixed_track_positions(
    records: Sequence[SrtRecord],
    frame_map: Mapping[str, object] | str,
    config: FixedTrackVisualPoseConfig,
) -> tuple[FixedTrackPosition, ...]:
    """Project valid SRT samples into CAD coordinates without visual position edits."""

    if not records:
        raise ValueError("SRT contains no parseable records")
    if not config.georeference.confirmed:
        raise ValueError("CAD georeference must be confirmed")
    timestamps, source_points = _frame_timestamps(frame_map, config)  # type: ignore[arg-type]
    samples = sample_srt_at_frames(
        records,
        frame_timestamps=timestamps,
        max_interpolation_gap_sec=config.max_interpolation_gap_sec,
    )
    valid_relative_height = sum(
        sample.rel_alt is not None and isfinite(float(sample.rel_alt))
        for sample in samples
    )
    if valid_relative_height == 0:
        raise ValueError(
            "SRT relative height is required; absolute height is diagnostic-only"
        )

    offset = tuple(float(value) for value in config.route_offset_xyz_m)
    positions: list[FixedTrackPosition] = []
    for frame_index, (sample, source_pts) in enumerate(zip(samples, source_points)):
        if not sample.gps_valid or sample.rel_alt is None:
            continue
        values = (
            sample.latitude,
            sample.longitude,
            sample.rel_alt,
        )
        if any(value is None or not isfinite(float(value)) for value in values):
            continue
        latitude = float(sample.latitude)
        longitude = float(sample.longitude)
        rel_alt = float(sample.rel_alt)
        cad_raw_x, cad_raw_y = project_wgs84_to_cad_raw(
            longitude, latitude, config.georeference
        )
        if (
            config.georeference.cad_axis_mapping
            == "cad_x_easting_cad_y_northing"
        ):
            easting, northing = cad_raw_x, cad_raw_y
        else:
            easting, northing = cad_raw_y, cad_raw_x
        local_x, local_y = cad_raw_to_local_m(
            (cad_raw_x, cad_raw_y), config.cad_origin_xy, config.cad_scale
        )
        canonical = (float(local_x), float(local_y), rel_alt)
        center = tuple(canonical[index] + offset[index] for index in range(3))
        abs_alt = (
            float(sample.abs_alt)
            if sample.abs_alt is not None and isfinite(float(sample.abs_alt))
            else None
        )
        positions.append(
            FixedTrackPosition(
                frame_index=frame_index,
                source_pts=int(source_pts),
                pts_time_sec=float(sample.frame_timestamp.pts_time_sec),
                canonical_center=canonical,
                center=center,
                latitude=latitude,
                longitude=longitude,
                rel_alt=rel_alt,
                abs_alt=abs_alt,
                projected_easting=float(easting),
                projected_northing=float(northing),
                cad_raw_x=float(cad_raw_x),
                cad_raw_y=float(cad_raw_y),
                interpolated=bool(sample.interpolated),
                source_entry_before=sample.source_entry_before,
                source_entry_after=sample.source_entry_after,
            )
        )

    coverage = len(positions) / len(timestamps)
    if coverage < config.minimum_position_coverage:
        raise ValueError(
            "fixed-track position coverage is below the configured minimum: "
            f"{coverage:.3f} < {config.minimum_position_coverage:.3f}"
        )
    max_speed = 0.0
    for first, second in zip(positions, positions[1:]):
        elapsed = second.pts_time_sec - first.pts_time_sec
        if elapsed <= 0.0:
            raise ValueError("fixed-track frame timestamps must be increasing")
        speed = hypot(
            second.canonical_center[0] - first.canonical_center[0],
            second.canonical_center[1] - first.canonical_center[1],
        ) / elapsed
        max_speed = max(max_speed, speed)
    if max_speed > config.max_horizontal_speed_mps:
        raise ValueError(
            "SRT horizontal speed exceeds the configured safety limit: "
            f"{max_speed:.3f} m/s"
        )
    return tuple(positions)
