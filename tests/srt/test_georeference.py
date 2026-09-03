from __future__ import annotations

import pytest

import cadscene.srt.georeference as georeference
from cadscene.srt.georeference import (
    CadGeoreference,
    cad_raw_to_local_m,
    project_wgs84_to_cad_raw,
    recommend_cgcs2000_candidates,
)


@pytest.mark.parametrize(
    "raw",
    ["118°50′", "118°50'", "118 50", "118.83333333333333", 118.83333333333333],
)
def test_degree_minute_central_meridian_normalizes_to_decimal(
    raw: object,
) -> None:
    assert georeference.parse_central_meridian(raw) == pytest.approx(
        118.0 + 50.0 / 60.0
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("118°60′", "minutes"),
        ("181°0′", "between -180 and 180"),
        ("invalid", "format"),
    ],
)
def test_invalid_central_meridian_text_is_rejected(
    raw: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        georeference.parse_central_meridian(raw)


def test_blank_central_meridian_keeps_automatic_recommendation() -> None:
    assert georeference.parse_central_meridian(None) is None
    assert georeference.parse_central_meridian("  ") is None


def _config(
    *,
    epsg: int = 4549,
    central_meridian_deg: float = 120.0,
    mapping: str = "cad_x_easting_cad_y_northing",
    confirmed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "horizontal_datum": "CGCS2000",
        "projection_family": "gauss_kruger",
        "zone_width_deg": 3,
        "central_meridian_deg": central_meridian_deg,
        "epsg": epsg,
        "projected_axis_order": "easting_northing",
        "cad_axis_mapping": mapping,
        "zone_prefix": False,
        "linear_unit": "metre",
        "source": "user_confirmed",
        "confirmed": confirmed,
        "confidence": 0.95,
        "validation": {"trajectory_inside_cad_ratio": 1.0},
    }


def _custom_config(
    *,
    mapping: str = "cad_x_easting_cad_y_northing",
    confirmed: bool = True,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "horizontal_datum": "CGCS2000",
        "projection_family": "gauss_kruger",
        "zone_width_deg": 3,
        "central_meridian_deg": 118.0 + 50.0 / 60.0,
        "epsg": None,
        "projected_axis_order": "easting_northing",
        "cad_axis_mapping": mapping,
        "zone_prefix": False,
        "linear_unit": "metre",
        "source": "user_confirmed",
        "confirmed": confirmed,
        "confidence": 0.95,
        "validation": {"trajectory_inside_cad_ratio": 1.0},
        "crs_source": "custom",
        "latitude_of_origin_deg": 0.0,
        "scale_factor": 1.0,
        "false_easting_m": 500_000.0,
        "false_northing_m": 0.0,
        "ellipsoid": "GRS80",
    }


def test_confirmed_epsg_4549_projects_with_explicit_axis_mapping() -> None:
    config = CadGeoreference.from_dict(_config())

    cad_x, cad_y = project_wgs84_to_cad_raw(120.0, 30.0, config)

    assert cad_x == pytest.approx(500_000.0, abs=0.01)
    assert cad_y == pytest.approx(3_320_113.397845, abs=0.01)


def test_unconfirmed_config_cannot_project() -> None:
    config = CadGeoreference.from_dict(_config(confirmed=False))

    with pytest.raises(ValueError, match="confirmed"):
        project_wgs84_to_cad_raw(120.0, 30.0, config)


def test_axis_swap_is_explicit_not_inherited_from_epsg_axis_order() -> None:
    east_north = project_wgs84_to_cad_raw(
        120.0,
        30.0,
        CadGeoreference.from_dict(_config()),
    )
    north_east = project_wgs84_to_cad_raw(
        120.0,
        30.0,
        CadGeoreference.from_dict(
            _config(mapping="cad_x_northing_cad_y_easting")
        ),
    )

    assert north_east == pytest.approx(tuple(reversed(east_north)))


def test_config_rejects_epsg_central_meridian_mismatch() -> None:
    with pytest.raises(ValueError, match="central_meridian_deg"):
        CadGeoreference.from_dict(
            _config(epsg=4549, central_meridian_deg=117.0)
        )


def test_cad_raw_to_local_m_uses_existing_origin_and_scale_contract() -> None:
    local = cad_raw_to_local_m(
        (500_010.0, 3_320_123.0),
        origin_xy=(500_000.0, 3_320_100.0),
        cad_scale=0.5,
    )

    assert local == pytest.approx((5.0, 11.5))


def test_candidate_near_120_recommends_4549_when_bbox_matches() -> None:
    candidates = recommend_cgcs2000_candidates(
        [119.999, 120.001],
        [30.0, 30.001],
        cad_bbox_raw=(499_800.0, 3_319_900.0, 500_200.0, 3_320_400.0),
    )

    assert candidates
    assert candidates[0].epsg == 4549
    assert candidates[0].central_meridian_deg == 120.0
    assert candidates[0].evidence["cad_bbox_raw"] == [
        499_800.0,
        3_319_900.0,
        500_200.0,
        3_320_400.0,
    ]
    assert candidates[0].evidence["trajectory_polyline_raw"]
    assert candidates[0].cad_axis_mapping == "cad_x_easting_cad_y_northing"
    assert candidates[0].confirmed is False


def test_other_region_does_not_reuse_120_degree_candidate() -> None:
    candidates = recommend_cgcs2000_candidates(
        [116.999, 117.001],
        [31.0, 31.001],
        cad_bbox_raw=(499_800.0, 3_430_000.0, 500_200.0, 3_431_000.0),
    )

    assert candidates
    assert candidates[0].central_meridian_deg == 117.0
    assert candidates[0].epsg == 4548
    assert candidates[0].epsg != 4549


def test_ambiguous_large_bbox_keeps_candidates_unconfirmed_with_axis_evidence() -> None:
    candidates = recommend_cgcs2000_candidates(
        [120.0, 120.001],
        [30.0, 30.001],
        cad_bbox_raw=(0.0, 0.0, 4_000_000.0, 4_000_000.0),
    )

    assert candidates
    assert candidates[0].confirmed is False
    assert candidates[0].evidence["axis_mapping"] in {
        "cad_x_easting_cad_y_northing",
        "cad_x_northing_cad_y_easting",
    }


def test_candidate_inputs_require_matching_finite_samples() -> None:
    with pytest.raises(ValueError, match="same non-zero length"):
        recommend_cgcs2000_candidates(
            [120.0],
            [],
            cad_bbox_raw=(0.0, 0.0, 1.0, 1.0),
        )


def test_manual_120_meridian_filters_every_candidate() -> None:
    candidates = recommend_cgcs2000_candidates(
        [119.999, 120.001],
        [30.0, 30.001],
        cad_bbox_raw=(499_800.0, 3_319_900.0, 500_200.0, 3_320_400.0),
        central_meridian_deg=120.0,
    )

    assert candidates
    assert {item.central_meridian_deg for item in candidates} == {120.0}


def test_candidate_progress_is_derived_from_scored_variants() -> None:
    events: list[tuple[str, str, float | None]] = []

    recommend_cgcs2000_candidates(
        [119.999, 120.001],
        [30.0, 30.001],
        cad_bbox_raw=(499_800.0, 3_319_900.0, 500_200.0, 3_320_400.0),
        central_meridian_deg=120.0,
        progress_callback=lambda stage, message, fraction: events.append(
            (stage, message, fraction)
        ),
    )

    scoring = [
        fraction
        for stage, _message, fraction in events
        if stage == "scoring_candidates"
    ]
    assert scoring
    assert scoring == sorted(scoring)
    assert scoring[-1] == 1.0
    assert [stage for stage, _message, _fraction in events] == [
        "validating_inputs",
        "enumerating_crs",
        *(["scoring_candidates"] * len(scoring)),
        "building_previews",
        "complete",
    ]


@pytest.mark.parametrize("central_meridian", [float("nan"), 181.0, -181.0])
def test_manual_meridian_must_be_finite_and_in_range(
    central_meridian: float,
) -> None:
    with pytest.raises(ValueError, match="central_meridian_deg"):
        recommend_cgcs2000_candidates(
            [120.0, 120.001],
            [30.0, 30.001],
            cad_bbox_raw=(0.0, 0.0, 1.0, 1.0),
            central_meridian_deg=central_meridian,
        )


def test_custom_118_degrees_50_minutes_candidate_uses_fixed_projection() -> None:
    central_meridian = georeference.parse_central_meridian("118°50′")

    candidates = recommend_cgcs2000_candidates(
        [119.071873],
        [28.896830],
        cad_bbox_raw=(484_000.0, 3_189_000.0, 550_000.0, 3_211_000.0),
        central_meridian_deg=central_meridian,
    )

    assert candidates
    assert candidates[0].crs_source == "custom"
    assert candidates[0].epsg is None
    assert candidates[0].central_meridian_deg == pytest.approx(118.0 + 50.0 / 60.0)
    assert candidates[0].evidence["trajectory_inside_cad_ratio"] == 1.0
    assert candidates[0].to_dict()["false_easting_m"] == 500_000.0


def test_confirmed_custom_projection_reuses_candidate_parameters() -> None:
    candidate = recommend_cgcs2000_candidates(
        [119.071873],
        [28.896830],
        cad_bbox_raw=(484_000.0, 3_189_000.0, 550_000.0, 3_211_000.0),
        central_meridian_deg=118.0 + 50.0 / 60.0,
    )[0]
    config = CadGeoreference.from_dict(
        {
            **_custom_config(),
            **{
                key: value
                for key, value in candidate.to_dict().items()
                if key not in {"crs_name", "score", "evidence"}
            },
            "confirmed": True,
        }
    )

    projected = project_wgs84_to_cad_raw(119.071873, 28.896830, config)

    assert projected == pytest.approx(
        candidate.evidence["trajectory_polyline_raw"][0]
    )
    assert 484_000.0 < projected[0] < 550_000.0
    assert 3_189_000.0 < projected[1] < 3_211_000.0
