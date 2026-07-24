from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from cadscene.alignment.rank1_constrained import Rank1Config, Rank1Transform
from cadscene.alignment.rank1_validation import validate_holdout_anchors
from cadscene.alignment.orientation_prior import OrientationPriorPackage
from cadscene.sfm.trajectory import SfmTrajectory


def _rotz(degrees: float) -> np.ndarray:
    angle = np.deg2rad(degrees)
    return np.asarray([[np.cos(angle), -np.sin(angle), 0.0], [np.sin(angle), np.cos(angle), 0.0], [0.0, 0.0, 1.0]])


def _trajectory() -> SfmTrajectory:
    return SfmTrajectory(
        frames=np.asarray([0, 10, 20], dtype=np.int64),
        centers=np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]),
        quats_c2w_wxyz=np.tile(np.asarray([1.0, 0.0, 0.0, 0.0]), (3, 1)),
        fps=30.0, width=1920, height=1080, intrinsics={"params": [1000.0]},
    )


def _prior(frame: int, position: np.ndarray, rotation: np.ndarray, role: str) -> OrientationPriorPackage:
    return OrientationPriorPackage(
        source_frame_index=frame, pts_time_sec=frame / 30.0,
        position_cad_m=np.asarray(position, dtype=np.float64),
        rotation_cad_from_camera=np.asarray(rotation, dtype=np.float64),
        camera_forward_cad=rotation[:, 2], camera_up_cad=-rotation[:, 1], camera_right_cad=rotation[:, 0],
        direction_type="camera_forward", direction_vector_cad=rotation[:, 2],
        orientation_source="manual", orientation_confirmed=True,
        yaw_confirmed=True, pitch_confirmed=True, roll_confirmed=False,
        solver_role=role, projection_residual_px=1.5,
    )


def _transform() -> Rank1Transform:
    return Rank1Transform(
        scale=2.0, rotation_cad_from_sfm=_rotz(90.0), translation_cad_from_sfm=np.asarray([5.0, 1.0, 3.0]),
        solve_frame_indices=(0,), rotation_disagreement_deg=0.0,
    )


def test_holdout_reports_position_components_and_orientation_angle():
    trajectory = _trajectory()
    transform = _transform()
    predicted = transform.apply_points(trajectory.centers)
    manual_position = predicted[2] + np.asarray([1.0, 2.0, -0.5])
    manual_rotation = _rotz(100.0)
    validate = _prior(20, manual_position, manual_rotation, "validate")

    report = validate_holdout_anchors(
        trajectory, predicted, transform, np.asarray([0.0, 1.0, 0.0]), [validate],
        Rank1Config(max_validate_position_error_m=5.0, max_validate_orientation_error_deg=15.0),
    )
    metric = report.metrics[0]

    assert metric.position_error_m == pytest.approx(np.linalg.norm([1.0, 2.0, -0.5]))
    assert metric.along_track_error_m == pytest.approx(-2.0)
    assert metric.cross_track_error_m == pytest.approx(1.0)
    assert metric.vertical_error_m == pytest.approx(0.5)
    assert metric.orientation_error_deg == pytest.approx(10.0)
    assert metric.projection_residual_px == pytest.approx(1.5)
    assert report.accepted


def test_holdout_along_error_uses_horizontal_projection_of_cad_direction():
    trajectory = _trajectory()
    transform = _transform()
    predicted = transform.apply_points(trajectory.centers)
    manual_position = predicted[2] - np.asarray([1.0, 0.0, 1.0])
    validate = _prior(20, manual_position, _rotz(90.0), "validate")

    report = validate_holdout_anchors(
        trajectory,
        predicted,
        transform,
        np.asarray([1.0, 0.0, 1.0]),
        [validate],
        Rank1Config(max_validate_position_error_m=5.0),
    )

    assert report.metrics[0].along_track_error_m == pytest.approx(1.0)
    assert report.metrics[0].cross_track_error_m == pytest.approx(0.0)
    assert report.metrics[0].vertical_error_m == pytest.approx(1.0)


def test_validate_anchor_never_changes_solve_transform():
    trajectory = _trajectory()
    transform = _transform()
    predicted = transform.apply_points(trajectory.centers)
    first = _prior(20, predicted[2], _rotz(90.0), "validate")
    second = replace(first, position_cad_m=np.asarray([999.0, -999.0, 500.0]))

    validate_holdout_anchors(trajectory, predicted, transform, np.asarray([0.0, 1.0, 0.0]), [first])
    validate_holdout_anchors(trajectory, predicted, transform, np.asarray([0.0, 1.0, 0.0]), [second])

    assert np.allclose(transform.rotation_cad_from_sfm, _rotz(90.0))
    assert np.allclose(transform.translation_cad_from_sfm, [5.0, 1.0, 3.0])


def test_missing_validate_anchor_is_diagnostic_only():
    trajectory = _trajectory()
    transform = _transform()
    report = validate_holdout_anchors(
        trajectory, transform.apply_points(trajectory.centers), transform,
        np.asarray([0.0, 1.0, 0.0]), [], Rank1Config(),
    )

    assert report.accepted is False
    assert report.holdout_missing is True
    assert report.rejection_reasons == ("holdout-missing",)
