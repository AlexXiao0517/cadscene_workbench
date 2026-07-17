from __future__ import annotations

from pathlib import Path

import numpy as np

from cadscene.core.io import write_csv_utf8_sig, write_json
from cadscene.diagnostics.road_surface import (
    classify_surface,
    extract_road_points_corridor,
    fit_global_plane,
    flat_plane_error,
    load_centerline,
    profile_surface_error,
    project_to_centerline,
    road_surface_profile,
    sfm_surface_quality,
    summarize_geometry,
)


def test_centerline_can_be_loaded_from_design_layer_with_dataset_transform(tmp_path: Path) -> None:
    write_json(
        tmp_path / "design.json",
        {
            "meta": {"coordinate_mode": "cad_world"},
            "layers": [
                {
                    "name": "future_road_center",
                    "kind": "center",
                    "entities": [{"world_points": [[100.0, 200.0], [120.0, 200.0]]}],
                }
            ],
        },
    )

    centerline = load_centerline(tmp_path, origin_xy=(100.0, 190.0), cad_scale=1.0)

    assert centerline is not None
    np.testing.assert_allclose(centerline.dense, [[0.0, 10.0], [20.0, 10.0]])


def test_station_projection_signed_lateral_and_corridor_extraction() -> None:
    center = np.asarray([[0.0, 0.0], [10.0, 0.0]], dtype=np.float64)
    points = np.asarray([[2.0, 3.0, 0.1], [4.0, -2.0, 0.2], [5.0, 20.0, 0.3]], dtype=np.float64)

    station, lateral, _nearest = project_to_centerline(points[:, :2], center)
    mask = extract_road_points_corridor(points, lateral, road_corridor_width=3.0)

    np.testing.assert_allclose(station, [2.0, 4.0, 5.0])
    np.testing.assert_allclose(lateral, [3.0, -2.0, 20.0])
    assert mask.tolist() == [True, True, False]


def test_station_binning_and_flat_error_csv_bom(tmp_path: Path) -> None:
    points = np.asarray([[1, 0, 0.0], [2, 0, 2.0], [12, 0, 4.0]], dtype=np.float64)
    station = np.asarray([1.0, 2.0, 12.0])
    rows = road_surface_profile(points, station, bin_m=10.0)

    assert rows[0]["point_count"] == 2
    assert rows[0]["median_z"] == 1.0
    assert flat_plane_error(points[:, 2])["rmse"] == np.sqrt((0.0 + 4.0 + 16.0) / 3.0)
    csv_path = tmp_path / "road_surface_profile.csv"
    write_csv_utf8_sig(csv_path, rows)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_global_plane_fit_recovers_simple_plane_and_profile_can_improve_flat() -> None:
    xs = np.linspace(0, 40, 9)
    points = np.asarray([[x, 0.0, 0.1 * x + 1.0] for x in xs], dtype=np.float64)
    plane = fit_global_plane(points)
    profile = profile_surface_error(points, xs, bin_m=10.0)

    assert plane["a"] == np.float64(0.1)
    assert plane["c"] == np.float64(1.0)
    assert profile["rmse"] < flat_plane_error(points[:, 2])["rmse"]


def test_classification_and_summary_conclusions_are_complete() -> None:
    assert classify_surface(road_point_count=2, flat_rmse=1, flat_p90=1, plane_rmse=1, profile_rmse=1)["road_flatness_status"] == "insufficient_points"
    tilted = classify_surface(road_point_count=300, flat_rmse=3.0, flat_p90=4.0, plane_rmse=0.2, profile_rmse=0.19)
    assert tilted["road_flatness_status"] == "globally_tilted"
    summary = summarize_geometry(
        road_point_count=300,
        road_point_ratio=0.5,
        flat={"rmse": 3.0, "p90_abs": 4.0},
        plane={"rmse": 0.2},
        profile={"rmse": 0.19},
        pose_warning={"pose_compensation_warning": False, "pose_compensation_reasons": []},
    )

    for key in [
        "road_flatness_status",
        "road_surface_reliability",
        "cad_flat_plane_assumption",
        "sfm_as_alignment_reference",
        "next_step_recommendation",
        "pose_compensation_warning",
        "pose_compensation_reasons",
    ]:
        assert key in summary["conclusions"]
    assert sfm_surface_quality(road_surface_profile(np.asarray([[0, 0, 0.0]], dtype=np.float64), np.asarray([0.0]), 10.0))[0]["risk_level"] in {"low", "medium", "high"}
