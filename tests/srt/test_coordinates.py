from __future__ import annotations

import pytest

from cadscene.srt.coordinates import build_local_enu


def test_east_north_use_fixed_reference_height_and_rel_alt_only_controls_up() -> None:
    points, meta = build_local_enu(
        [
            {"latitude": 0.0, "longitude": 0.0, "rel_alt": 50.0, "abs_alt": 100.0},
            {"latitude": 0.0, "longitude": 0.001, "rel_alt": 53.0, "abs_alt": 110.0},
        ]
    )

    assert points[0].east_m == pytest.approx(0.0, abs=1e-8)
    assert points[1].east_m == pytest.approx(111.319, rel=1e-3)
    assert points[1].north_m == pytest.approx(0.0, abs=1e-6)
    assert points[1].up_m == pytest.approx(3.0)
    assert meta["ecef_reference_height_source"] == "first_abs_alt_unverified"
    assert meta["up_source"] == "rel_alt_relative"
    assert meta["absolute_elevation_available"] is False


def test_abs_alt_fallback_remains_relative_and_zero_is_explicit() -> None:
    points, meta = build_local_enu(
        [
            {"latitude": 30.0, "longitude": 120.0, "abs_alt": 120.0},
            {"latitude": 30.0001, "longitude": 120.0, "abs_alt": 124.0},
        ],
        ecef_reference_height_source="zero",
    )

    assert points[1].up_m == pytest.approx(4.0)
    assert meta["ecef_reference_height_source"] == "zero"
    assert meta["ecef_reference_height_m"] == 0.0
    assert meta["up_source"] == "abs_alt_relative"
