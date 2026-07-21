from __future__ import annotations

import numpy as np
import pytest

from cadscene.alignment.rank1_constrained import (
    AlongTrackScaleFit,
    Rank1Config,
    analyze_rank1_axis,
    estimate_along_track_scale,
    project_along_track,
    solve_rank1_transform,
)
from cadscene.alignment.orientation_prior import OrientationPriorPackage
from cadscene.sfm.trajectory import SfmTrajectory


def test_rank1_axis_uses_time_order_to_resolve_pca_sign():
    times = np.arange(8, dtype=np.float64)
    points = np.column_stack([-2.0 * times, np.zeros(8), np.zeros(8)])

    analysis = analyze_rank1_axis(points, times, Rank1Config(min_baseline=1.0))

    assert analysis.rank == 1
    assert analysis.near_linear is True
    assert float(np.dot(analysis.primary_direction, points[-1] - points[0])) > 0.0
    assert analysis.direction_sign_source == "time_forward_endpoint_delta"


def test_project_along_track_uses_explicit_reference():
    points = np.asarray([[5.0, 2.0, 0.0], [8.0, 2.0, 0.0]])
    result = project_along_track(points, np.asarray([1.0, 0.0, 0.0]), points[0])
    assert np.allclose(result, [0.0, 3.0])


def test_robust_scale_recovers_known_metric_scale_and_residuals():
    u_sfm = np.linspace(-5.0, 7.0, 25)
    u_srt = 3.25 * u_sfm + 18.0

    fit = estimate_along_track_scale(u_sfm, u_srt, Rank1Config(ransac_seed=17))

    assert fit.scale == pytest.approx(3.25, rel=1e-8)
    assert fit.offset == pytest.approx(18.0, abs=1e-8)
    assert fit.inlier_count == len(u_sfm)
    assert fit.median_along_track_residual_m == pytest.approx(0.0, abs=1e-8)


def test_endpoint_outliers_do_not_control_scale():
    u_sfm = np.arange(30, dtype=np.float64)
    u_srt = 2.0 * u_sfm + 4.0
    u_srt[0] -= 120.0
    u_srt[-1] += 150.0

    fit = estimate_along_track_scale(
        u_sfm,
        u_srt,
        Rank1Config(ransac_seed=4, along_track_inlier_threshold_m=0.25),
    )

    assert fit.scale == pytest.approx(2.0, rel=1e-8)
    assert fit.inlier_count == 28
    assert fit.inlier_ratio == pytest.approx(28 / 30)


def test_rank_two_input_is_rejected_by_rank1_gate():
    t = np.linspace(0.0, 2.0 * np.pi, 30)
    points = np.column_stack([20.0 * np.cos(t), 10.0 * np.sin(t), np.zeros_like(t)])

    with pytest.raises(ValueError, match="rank=1"):
        analyze_rank1_axis(points, t, Rank1Config(min_baseline=1.0))


def _rotz(degrees: float) -> np.ndarray:
    angle = np.deg2rad(degrees)
    return np.asarray([[np.cos(angle), -np.sin(angle), 0.0], [np.sin(angle), np.cos(angle), 0.0], [0.0, 0.0, 1.0]])


def _trajectory(centers: np.ndarray) -> SfmTrajectory:
    return SfmTrajectory(
        frames=np.arange(len(centers), dtype=np.int64) * 10,
        centers=np.asarray(centers, dtype=np.float64),
        quats_c2w_wxyz=np.tile(np.asarray([1.0, 0.0, 0.0, 0.0]), (len(centers), 1)),
        fps=30.0,
        width=1920,
        height=1080,
        intrinsics={"params": [1000.0]},
    )


def _scale_fit(scale: float) -> AlongTrackScaleFit:
    return AlongTrackScaleFit(scale, 0.0, (True,) * 8, 8, 1.0, 0.0, 0.0, 0.0, 0.0)


def _prior(frame: int, rotation: np.ndarray, position: np.ndarray, *, direction=None) -> OrientationPriorPackage:
    right, down, forward = rotation[:, 0], rotation[:, 1], rotation[:, 2]
    return OrientationPriorPackage(
        source_frame_index=frame,
        pts_time_sec=frame / 30.0,
        position_cad_m=np.asarray(position, dtype=np.float64),
        rotation_cad_from_camera=np.asarray(rotation, dtype=np.float64),
        camera_forward_cad=forward,
        camera_up_cad=-down,
        camera_right_cad=right,
        direction_type="camera_forward",
        direction_vector_cad=np.asarray(forward if direction is None else direction, dtype=np.float64),
        orientation_source="manual",
        orientation_confirmed=True,
        yaw_confirmed=True,
        pitch_confirmed=True,
        roll_confirmed=False,
        solver_role="solve",
        projection_residual_px=None,
    )


def test_solve_prior_recovers_known_rotation_and_translation():
    centers = np.column_stack([np.arange(8, dtype=np.float64), np.zeros(8), np.zeros(8)])
    trajectory = _trajectory(centers)
    expected_rotation = _rotz(90.0)
    expected_translation = np.asarray([12.0, -7.0, 3.0])
    solve_index = 30
    source_center, _ = trajectory.query(solve_index)
    manual_center = 2.5 * (expected_rotation @ source_center) + expected_translation
    prior = _prior(solve_index, expected_rotation, manual_center)

    transform = solve_rank1_transform(
        trajectory,
        _scale_fit(2.5),
        np.asarray([1.0, 0.0, 0.0]),
        [prior],
        Rank1Config(min_baseline=1.0),
    )

    assert transform.scale == pytest.approx(2.5)
    assert np.allclose(transform.rotation_cad_from_sfm, expected_rotation)
    assert np.allclose(transform.translation_cad_from_sfm, expected_translation)
    assert np.allclose(transform.apply_points(centers), 2.5 * (centers @ expected_rotation.T) + expected_translation)


def test_solve_rejects_missing_frame_and_parallel_prior():
    trajectory = _trajectory(np.column_stack([np.arange(8), np.zeros(8), np.zeros(8)]))
    missing = _prior(999, _rotz(90.0), np.zeros(3))
    parallel = _prior(20, _rotz(90.0), np.zeros(3), direction=np.asarray([0.0, 1.0, 0.0]))

    with pytest.raises(ValueError, match="not registered"):
        solve_rank1_transform(trajectory, _scale_fit(1.0), np.asarray([1.0, 0.0, 0.0]), [missing])
    with pytest.raises(ValueError, match="near-parallel"):
        solve_rank1_transform(trajectory, _scale_fit(1.0), np.asarray([1.0, 0.0, 0.0]), [parallel])


def test_inconsistent_multiple_solve_priors_are_rejected():
    trajectory = _trajectory(np.column_stack([np.arange(8), np.zeros(8), np.zeros(8)]))
    first_rotation = _rotz(90.0)
    second_rotation = _rotz(120.0)
    first = _prior(10, first_rotation, first_rotation @ trajectory.centers[1])
    second = _prior(50, second_rotation, second_rotation @ trajectory.centers[5])

    with pytest.raises(ValueError, match="rotation disagreement"):
        solve_rank1_transform(
            trajectory,
            _scale_fit(1.0),
            np.asarray([1.0, 0.0, 0.0]),
            [first, second],
            Rank1Config(max_solve_rotation_disagreement_deg=5.0),
        )
