from __future__ import annotations

from fractions import Fraction

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackPosition,
    FixedTrackVisualPoseConfig,
    PairRotationMeasurement,
    build_fixed_track_positions,
    estimate_video_orientations,
    interpolate_orientations,
    solve_fixed_center_rotations,
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


def test_high_rate_quantized_gps_does_not_trigger_instantaneous_speed_limit() -> None:
    frame_count = 61
    records = [
        SrtRecord(
            start_sec=index / 60.0,
            end_sec=(index + 1) / 60.0,
            latitude=30.0,
            longitude=120.0 + (index // 5) * 0.00002,
            rel_alt=80.0,
        )
        for index in range(frame_count)
    ]
    frame_map = {
        "source_time_base": {"numerator": 1, "denominator": 60},
        "clips": [
            {
                "clip_id": "clip-1",
                "source_start_pts": 0,
                "source_end_pts_exclusive": frame_count,
                "frames": [{"pts": index} for index in range(frame_count)],
            }
        ],
    }
    config = _config(
        source_end_pts_exclusive=frame_count,
        source_time_base=Fraction(1, 60),
        max_horizontal_speed_mps=100.0,
    )

    positions = build_fixed_track_positions(records, frame_map, config)

    assert len(positions) == frame_count


def test_high_rate_persistent_gps_jump_still_triggers_speed_limit() -> None:
    frame_count = 61
    records = [
        SrtRecord(
            start_sec=index / 60.0,
            end_sec=(index + 1) / 60.0,
            latitude=30.0,
            longitude=120.0 + (0.001 if index >= 30 else 0.0),
            rel_alt=80.0,
        )
        for index in range(frame_count)
    ]
    frame_map = {
        "source_time_base": {"numerator": 1, "denominator": 60},
        "clips": [
            {
                "clip_id": "clip-1",
                "source_start_pts": 0,
                "source_end_pts_exclusive": frame_count,
                "frames": [{"pts": index} for index in range(frame_count)],
            }
        ],
    }
    config = _config(
        source_end_pts_exclusive=frame_count,
        source_time_base=Fraction(1, 60),
        max_horizontal_speed_mps=100.0,
    )

    with pytest.raises(ValueError, match="horizontal speed"):
        build_fixed_track_positions(records, frame_map, config)


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


def _measurement(
    first: int,
    second: int,
    centers: dict[int, np.ndarray],
    rotations: dict[int, np.ndarray],
) -> PairRotationMeasurement:
    relative = rotations[second] @ rotations[first].T
    baseline = centers[first] - centers[second]
    direction = rotations[second] @ baseline
    direction /= np.linalg.norm(direction)
    return PairRotationMeasurement(
        first_frame=first,
        second_frame=second,
        rotation_second_from_first=relative,
        translation_direction_second=direction,
        inlier_count=80,
    )


def _rotation_error_degrees(actual: np.ndarray, expected: np.ndarray) -> float:
    delta = Rotation.from_matrix(actual @ expected.T)
    return float(np.degrees(np.linalg.norm(delta.as_rotvec())))


def test_rotation_solver_recovers_world_attitudes_without_changing_centers() -> None:
    centers = {
        0: np.asarray([0.0, 0.0, 80.0]),
        1: np.asarray([2.0, 0.0, 80.2]),
        2: np.asarray([3.0, 1.5, 80.5]),
        3: np.asarray([4.0, 3.0, 80.6]),
    }
    root = Rotation.from_euler("zyx", [25.0, -12.0, 4.0], degrees=True).as_matrix()
    rotations = {
        index: Rotation.from_euler("z", index * 3.0, degrees=True).as_matrix()
        @ root
        for index in centers
    }
    measurements = tuple(
        _measurement(first, second, centers, rotations)
        for first, second in zip(range(3), range(1, 4))
    )
    frozen = {frame: center.copy() for frame, center in centers.items()}

    solution = solve_fixed_center_rotations(centers, measurements)

    assert solution.status == "orientation_ready"
    assert set(solution.rotations) == set(rotations)
    assert max(
        _rotation_error_degrees(solution.rotations[index], rotations[index])
        for index in rotations
    ) < 1e-6
    for frame in centers:
        np.testing.assert_array_equal(centers[frame], frozen[frame])


def test_straight_track_reports_position_only_instead_of_guessing_attitude() -> None:
    centers = {
        index: np.asarray([float(index), 0.0, 80.0]) for index in range(4)
    }
    rotations = {index: np.eye(3) for index in centers}
    measurements = tuple(
        _measurement(first, second, centers, rotations)
        for first, second in zip(range(3), range(1, 4))
    )

    solution = solve_fixed_center_rotations(centers, measurements)

    assert solution.status == "position_only"
    assert solution.rotations == {}
    assert set(solution.relative_rotations) == set(centers)
    assert solution.component_ids == {0: 0, 1: 0, 2: 0, 3: 0}
    assert solution.recommended_anchor_frame == 1
    np.testing.assert_allclose(solution.relative_rotations[0], np.eye(3), atol=1e-12)
    assert any("unobservable" in warning.lower() for warning in solution.warnings)


def test_no_visual_pairs_do_not_create_a_false_anchor_recommendation() -> None:
    solution = solve_fixed_center_rotations(
        {
            0: np.asarray([0.0, 0.0, 80.0]),
            1: np.asarray([1.0, 0.0, 80.0]),
        },
        (),
    )

    assert solution.relative_rotations == {}
    assert solution.component_ids == {}
    assert solution.recommended_anchor_frame is None


def test_orientation_interpolation_uses_slerp_only_inside_bounded_gap() -> None:
    rotations = {
        0: np.eye(3),
        10: Rotation.from_euler("z", 90.0, degrees=True).as_matrix(),
    }
    times = {frame: frame / 10.0 for frame in range(11)}

    bounded = interpolate_orientations(rotations, times, max_gap_sec=1.0)
    blocked = interpolate_orientations(rotations, times, max_gap_sec=0.5)

    expected_half = Rotation.from_euler("z", 45.0, degrees=True).as_matrix()
    assert _rotation_error_degrees(bounded[5], expected_half) < 1e-8
    assert blocked[5] is None
    np.testing.assert_allclose(blocked[0], rotations[0], atol=1e-12)
    np.testing.assert_allclose(blocked[10], rotations[10], atol=1e-12)


def test_video_orientation_estimation_samples_clip_and_preserves_srt_centers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cv2
    import cadscene.srt.fixed_track_visual_pose as subject

    centers = {
        0: np.asarray([0.0, 0.0, 80.0]),
        1: np.asarray([2.0, 0.0, 80.2]),
        2: np.asarray([3.0, 1.5, 80.5]),
        3: np.asarray([4.0, 3.0, 80.6]),
    }
    rotations = {
        index: Rotation.from_euler("zyx", [20.0 + 3.0 * index, -8.0, 2.0], degrees=True).as_matrix()
        for index in centers
    }
    positions = tuple(
        FixedTrackPosition(
            frame_index=index,
            source_pts=index,
            pts_time_sec=index * 0.5,
            canonical_center=tuple(center),
            center=tuple(center),
            latitude=30.0,
            longitude=120.0,
            rel_alt=float(center[2]),
            abs_alt=230.0,
            projected_easting=float(center[0]),
            projected_northing=float(center[1]),
            cad_raw_x=float(center[0]),
            cad_raw_y=float(center[1]),
            interpolated=False,
            source_entry_before=index,
            source_entry_after=index,
        )
        for index, center in centers.items()
    )

    class FakeCapture:
        def __init__(self, _path: str) -> None:
            self.index = 0

        def isOpened(self) -> bool:
            return True

        def read(self):
            if self.index >= len(positions):
                return False, None
            image = np.full((24, 32, 3), self.index, dtype=np.uint8)
            self.index += 1
            return True, image

        def release(self) -> None:
            return None

    observed_pairs: list[tuple[int, int]] = []

    def fake_extract(first_index, second_index, _first, _second, _camera, **_kwargs):
        observed_pairs.append((first_index, second_index))
        return _measurement(first_index, second_index, centers, rotations), {
            "first_frame": first_index,
            "second_frame": second_index,
            "status": "accepted",
        }

    monkeypatch.setattr(cv2, "VideoCapture", FakeCapture)
    monkeypatch.setattr(subject, "_extract_pair_rotation_measurement", fake_extract)
    frozen = {frame: center.copy() for frame, center in centers.items()}

    solution = estimate_video_orientations(
        "clip.mp4",
        positions,
        {
            "model": "PINHOLE",
            "width": 32,
            "height": 24,
            "params": [30.0, 30.0, 16.0, 12.0],
        },
        _config(keyframe_interval_sec=0.5),
    )

    assert solution.status == "orientation_ready"
    assert set(solution.relative_rotations) == set(centers)
    assert solution.recommended_anchor_frame in centers
    assert observed_pairs == [(0, 1), (1, 2), (2, 3)]
    assert all(row["status"] == "accepted" for row in solution.diagnostics[-3:])
    for frame in centers:
        np.testing.assert_array_equal(centers[frame], frozen[frame])
