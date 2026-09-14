from __future__ import annotations

import numpy as np
import pytest
from pyproj import Transformer

from cadscene.diagnostics.xml_at_reference import (
    bentley_ypr_world_to_camera_rotation,
    build_adjusted_at_track,
    project_distorted_segments,
    srt_vertical_reference_m,
)
from cadscene.srt.bentley_pose_merge import BentleyCameraModel, BentleyPoseSample
from cadscene.srt.georeference import CadGeoreference, project_wgs84_to_cad_raw
from cadscene.srt.schema import SrtRecord


def _camera_model() -> BentleyCameraModel:
    return BentleyCameraModel(
        image_size=(1000, 500),
        focal_length_mm=10.0,
        sensor_size_mm=20.0,
        principal_point_px=(501.0, 249.0),
        distortion=(0.1, 0.01, 0.001, 0.002, -0.003),
        aspect_ratio=1.0,
        skew=0.0,
    )


def _georeference() -> CadGeoreference:
    return CadGeoreference(
        schema_version=1,
        horizontal_datum="CGCS2000",
        projection_family="gauss_kruger",
        zone_width_deg=3,
        central_meridian_deg=118.83333333333333,
        epsg=None,
        projected_axis_order="easting_northing",
        cad_axis_mapping="cad_x_easting_cad_y_northing",
        zone_prefix=False,
        linear_unit="metre",
        source="test",
        confirmed=True,
        confidence=1.0,
        crs_source="custom",
    )


def test_srt_vertical_reference_uses_median_absolute_minus_relative_altitude() -> None:
    records = (
        SrtRecord(0.0, 0.1, abs_alt=230.0, rel_alt=85.0),
        SrtRecord(0.1, 0.2, abs_alt=232.0, rel_alt=86.0),
        SrtRecord(0.2, 0.3, abs_alt=None, rel_alt=90.0),
    )

    assert srt_vertical_reference_m(records) == pytest.approx(145.5)


def test_build_adjusted_track_interpolates_ecef_and_shortest_arc_rotation() -> None:
    georeference = _georeference()
    first_center = (118.8000, 28.9000, 230.0)
    second_center = (118.8002, 28.9002, 232.0)
    origin = project_wgs84_to_cad_raw(first_center[0], first_center[1], georeference)
    samples = (
        BentleyPoseSample(0, 170.0, -45.0, 1.0, None, first_center),
        BentleyPoseSample(2, -170.0, -45.0, 1.0, None, second_center),
    )

    poses = build_adjusted_at_track(
        samples,
        (0, 1, 2),
        georeference=georeference,
        cad_origin_xy=origin,
        cad_scale=1.0,
        vertical_reference_m=145.0,
        fov_deg=60.0,
    )

    to_ecef = Transformer.from_crs(4979, 4978, always_xy=True)
    from_ecef = Transformer.from_crs(4978, 4979, always_xy=True)
    first_ecef = np.asarray(to_ecef.transform(*first_center), dtype=np.float64)
    second_ecef = np.asarray(to_ecef.transform(*second_center), dtype=np.float64)
    expected_midpoint = from_ecef.transform(*((first_ecef + second_ecef) / 2.0))

    assert poses[0].camera.camera_x == pytest.approx(0.0, abs=1e-6)
    assert poses[0].camera.camera_y == pytest.approx(0.0, abs=1e-6)
    assert poses[0].camera.camera_z == pytest.approx(85.0)
    assert poses[0].camera.pitch_deg == pytest.approx(45.0)
    assert poses[0].camera.roll_deg == pytest.approx(-1.0)
    assert abs(abs(poses[1].camera.yaw_deg) - 180.0) < 1e-6
    assert poses[1].camera.pitch_deg == pytest.approx(45.0)
    assert poses[1].longitude == pytest.approx(expected_midpoint[0], abs=1e-10)
    assert poses[1].latitude == pytest.approx(expected_midpoint[1], abs=1e-10)
    assert poses[1].adjusted_altitude_m == pytest.approx(expected_midpoint[2], abs=1e-6)


def test_bentley_ypr_rotation_matches_x_right_y_down_manual_formula() -> None:
    rotation = bentley_ypr_world_to_camera_rotation(
        yaw_deg=0.0,
        pitch_deg=0.0,
        roll_deg=30.0,
    )

    root_three_over_two = np.sqrt(3.0) / 2.0
    assert rotation == pytest.approx(
        np.asarray(
            [
                [root_three_over_two, 0.0, 0.5],
                [0.5, 0.0, -root_three_over_two],
                [0.0, 1.0, 0.0],
            ]
        )
    )


def test_build_adjusted_track_requires_adjusted_xml_centers() -> None:
    sample = BentleyPoseSample(0, 0.0, -45.0, 0.0, None, None)

    with pytest.raises(ValueError, match="adjusted.*center"):
        build_adjusted_at_track(
            (sample,),
            (0,),
            georeference=_georeference(),
            cad_origin_xy=(0.0, 0.0),
            cad_scale=1.0,
            vertical_reference_m=0.0,
            fov_deg=60.0,
        )


def test_project_distorted_segments_applies_brown_conrady_and_principal_point() -> None:
    model = _camera_model()
    starts = np.asarray([[0.1, 0.0, 1.0]], dtype=np.float64)
    ends = np.asarray([[0.2, 0.0, 1.0]], dtype=np.float64)

    projection = project_distorted_segments(starts, ends, model)

    fx, _fy = model.focal_pixels
    cx, cy = model.principal_point_px
    k1, k2, k3, p1, p2 = model.distortion
    x, y = 0.1, 0.0
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    assert projection.uv_starts[0, 0] == pytest.approx(cx + fx * xd)
    assert projection.uv_starts[0, 1] == pytest.approx(cy + fx * yd)
    assert projection.depth_starts.tolist() == pytest.approx([1.0])
    assert projection.depth_ends.tolist() == pytest.approx([1.0])


def test_project_distorted_segments_rejects_wrong_shape() -> None:
    with pytest.raises(ValueError, match="shape"):
        project_distorted_segments(
            np.asarray([0.0, 0.0, 1.0]),
            np.asarray([[0.0, 0.0, 1.0]]),
            _camera_model(),
        )
