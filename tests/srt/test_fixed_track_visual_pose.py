from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest

from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackVisualPoseConfig,
    build_fixed_track_positions,
)
from cadscene.srt.georeference import (
    CadGeoreference,
    project_wgs84_to_cad_raw,
)
from cadscene.srt.schema import SrtRecord


def _georeference() -> CadGeoreference:
    return CadGeoreference.from_dict(
        {
            "schema_version": 1,
            "horizontal_datum": "CGCS2000",
            "projection_family": "gauss_kruger",
            "zone_width_deg": 3,
            "central_meridian_deg": 120.0,
            "epsg": 4549,
            "projected_axis_order": "easting_northing",
            "cad_axis_mapping": "cad_x_easting_cad_y_northing",
            "zone_prefix": False,
            "linear_unit": "metre",
            "source": "test",
            "confirmed": True,
            "confidence": 1.0,
        }
    )


def _records(*, relative: bool = True) -> list[SrtRecord]:
    return [
        SrtRecord(
            start_sec=float(index),
            end_sec=float(index + 1),
            latitude=30.0 + index * 0.00001,
            longitude=120.0 + index * 0.00001,
            rel_alt=80.0 + index if relative else None,
            abs_alt=230.0 + index,
        )
        for index in range(3)
    ]


def _frame_map() -> dict:
    return {
        "source_time_base": {"numerator": 1, "denominator": 1},
        "clips": [
            {
                "clip_id": "clip-1",
                "source_start_pts": 0,
                "source_end_pts_exclusive": 3,
                "frames": [{"pts": index} for index in range(3)],
            }
        ],
    }


def _config(**overrides) -> FixedTrackVisualPoseConfig:
    georeference = _georeference()
    first_x, first_y = project_wgs84_to_cad_raw(120.0, 30.0, georeference)
    values = {
        "clip_id": "clip-1",
        "source_start_pts": 0,
        "source_end_pts_exclusive": 3,
        "source_time_base": Fraction(1, 1),
        "georeference": georeference,
        "cad_origin_xy": (first_x, first_y),
        "cad_scale": 1.0,
        "horizontal_fov_deg": 72.0,
        "route_offset_xyz_m": (3.0, -2.0, 7.5),
    }
    values.update(overrides)
    return FixedTrackVisualPoseConfig(**values)


def test_positions_use_projected_xy_relative_alt_and_one_route_offset() -> None:
    positions = build_fixed_track_positions(_records(), _frame_map(), _config())

    assert len(positions) == 3
    assert positions[0].canonical_center == pytest.approx((0.0, 0.0, 80.0))
    assert positions[0].center == pytest.approx((3.0, -2.0, 87.5))
    np.testing.assert_allclose(
        np.asarray(positions[1].center) - np.asarray(positions[1].canonical_center),
        [3.0, -2.0, 7.5],
    )
    assert positions[0].abs_alt == pytest.approx(230.0)
    assert positions[0].height_source == "rel_alt"


def test_absolute_height_without_relative_height_is_rejected() -> None:
    with pytest.raises(ValueError, match="relative height"):
        build_fixed_track_positions(_records(relative=False), _frame_map(), _config())


@pytest.mark.parametrize(
    "overrides",
    [
        {"horizontal_fov_deg": 179.0},
        {"route_offset_xyz_m": (0.0, float("nan"), 0.0)},
        {"cad_scale": 0.0},
    ],
)
def test_fixed_track_config_rejects_invalid_numeric_contract(overrides) -> None:
    with pytest.raises(ValueError):
        _config(**overrides)


def test_config_round_trip_preserves_uniform_xyz_offset() -> None:
    original = _config(route_offset_xyz_m=(1.25, -3.5, 8.0))

    restored = FixedTrackVisualPoseConfig.from_dict(original.to_dict())

    assert restored == original
