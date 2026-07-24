from __future__ import annotations

import numpy as np
import pytest

from cadscene.alignment.rank1_constrained import (
    AlongTrackScaleFit,
    Rank1Config,
    apply_along_track_correction,
    apply_vertical_srt_constraint,
    analyze_rank1_axis,
    build_rank1_trajectory_json,
    cad_track_basis,
    estimate_along_track_scale,
    project_along_track,
    solve_rank1_transform,
)
from cadscene.alignment.orientation_prior import OrientationPriorPackage
from cadscene.sfm.trajectory import SfmTrajectory
from cadscene.sfm.trajectory import load_sfm_trajectory, quat_wxyz_to_matrix


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


def test_cad_track_basis_removes_vertical_component_from_horizontal_axes():
    along, lateral, vertical = cad_track_basis(np.asarray([3.0, 4.0, 12.0]))

    assert np.allclose(along, [0.6, 0.8, 0.0])
    assert np.allclose(lateral, [-0.8, 0.6, 0.0])
    assert np.allclose(vertical, [0.0, 0.0, 1.0])
    assert np.allclose(np.asarray([along, lateral, vertical]) @ np.asarray([along, lateral, vertical]).T, np.eye(3))


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


def test_multiple_solve_consistency_reports_horizontal_spread_and_height_offset_range_mad():
    centers = np.column_stack(
        [np.arange(8, dtype=np.float64), np.zeros(8), np.linspace(0.0, 4.0, 8)]
    )
    trajectory = _trajectory(centers)
    first = _prior(10, np.eye(3), np.asarray([1.0, 0.0, 10.0]))
    second = _prior(50, np.eye(3), np.asarray([5.0, 0.0, 10.345]))

    transform = solve_rank1_transform(
        trajectory,
        _scale_fit(1.0),
        np.asarray([1.0, 0.0, 0.0]),
        [first, second],
        Rank1Config(
            max_along_translation_spread_m=2.0,
            max_lateral_translation_spread_m=2.0,
            max_solve_height_offset_range_m=3.0,
            max_solve_height_offset_mad_m=1.5,
        ),
        srt_relative_height_by_frame={10: 0.0, 50: 0.0},
    )

    assert transform.along_translation_spread_m == pytest.approx(0.0)
    assert transform.lateral_translation_spread_m == pytest.approx(0.0)
    assert transform.solve_height_offset_range_m == pytest.approx(0.345)
    assert transform.solve_height_offset_mad_m == pytest.approx(0.1725)
    assert transform.height_offset_m == pytest.approx(10.1725)


def test_srt_correction_moves_positions_along_cad_axis_only():
    times = np.arange(7, dtype=np.float64)
    base = np.column_stack([times, np.full(7, 5.0), np.full(7, 2.0)])
    target_u = times + 3.0
    result = apply_along_track_correction(
        base,
        np.asarray([1.0, 0.0, 0.0]),
        times,
        target_u,
        np.ones(7, dtype=bool),
        Rank1Config(smoothing_window_sec=3.0, min_smoothing_support=2),
    )

    assert np.allclose(result.fused_positions[:, 0], times + 3.0)
    assert np.allclose(result.fused_positions[:, 1:], base[:, 1:])


def test_lateral_srt_noise_and_single_jump_are_not_copied():
    times = np.arange(9, dtype=np.float64)
    base = np.column_stack([times, np.zeros(9), np.zeros(9)])
    srt = base.copy()
    srt[:, 1] = np.asarray([20.0, -18.0, 15.0, -22.0, 19.0, -16.0, 21.0, -17.0, 14.0])
    target_u = project_along_track(srt, np.asarray([1.0, 0.0, 0.0]), np.zeros(3))
    target_u[4] += 100.0

    result = apply_along_track_correction(
        base,
        np.asarray([1.0, 0.0, 0.0]),
        times,
        target_u,
        np.ones(9, dtype=bool),
        Rank1Config(smoothing_window_sec=4.0, min_smoothing_support=3),
    )

    assert np.allclose(result.fused_positions, base)
    assert result.raw_delta_u_m[4] == pytest.approx(100.0)
    assert result.smoothed_delta_u_m[4] == pytest.approx(0.0)


def test_srt_hole_is_not_corrected_or_bridged():
    times = np.arange(7, dtype=np.float64)
    base = np.column_stack([times, np.zeros(7), np.zeros(7)])
    target_u = times + np.asarray([2.0, 2.0, 2.0, 100.0, -3.0, -3.0, -3.0])
    valid = np.asarray([True, True, True, False, True, True, True])

    result = apply_along_track_correction(
        base,
        np.asarray([1.0, 0.0, 0.0]),
        times,
        target_u,
        valid,
        Rank1Config(smoothing_window_sec=20.0, min_smoothing_support=2),
    )

    assert result.smoothing_support[3] == 0
    assert np.allclose(result.fused_positions[3], base[3])
    assert np.allclose(result.smoothed_delta_u_m[:3], 2.0)
    assert np.allclose(result.smoothed_delta_u_m[4:], -3.0)


def test_vertical_constraint_uses_srt_low_frequency_and_clamps_sfm_detail():
    times = np.arange(9, dtype=np.float64)
    base = np.column_stack([times, np.zeros(9), np.linspace(0.0, 8.0, 9)])
    relative_height = np.zeros(9, dtype=np.float64)

    result = apply_vertical_srt_constraint(
        base,
        times,
        relative_height,
        np.ones(9, dtype=bool),
        height_offset_m=10.0,
        config=Rank1Config(
            vertical_smoothing_window_sec=20.0,
            min_vertical_smoothing_support=3,
            max_sfm_vertical_detail_m=0.5,
        ),
    )

    assert np.ptp(result.fused_positions[:, 2]) == pytest.approx(1.0)
    assert result.fused_positions[0, 2] == pytest.approx(9.5)
    assert result.fused_positions[-1, 2] == pytest.approx(10.5)
    assert np.max(np.abs(result.sfm_vertical_detail_m)) == pytest.approx(0.5)
    assert np.allclose(result.fused_positions[:, :2], base[:, :2])


def test_vertical_constraint_suppresses_height_jump_and_does_not_bridge_hole():
    times = np.arange(9, dtype=np.float64)
    base = np.column_stack([times, np.zeros(9), np.full(9, 3.0)])
    relative_height = np.asarray([0.0, 0.0, 0.0, 100.0, np.nan, 2.0, 2.0, 2.0, 2.0])
    valid = np.asarray([True, True, True, True, False, True, True, True, True])

    result = apply_vertical_srt_constraint(
        base,
        times,
        relative_height,
        valid,
        height_offset_m=10.0,
        config=Rank1Config(
            vertical_smoothing_window_sec=20.0,
            min_vertical_smoothing_support=3,
            max_sfm_vertical_detail_m=0.5,
        ),
    )

    assert np.allclose(result.fused_positions[:4, 2], 10.0)
    assert result.fused_positions[4, 2] == pytest.approx(base[4, 2])
    assert result.vertical_smoothing_support[4] == 0
    assert np.allclose(result.fused_positions[5:, 2], 12.0)
    assert np.all(result.vertical_smoothing_support[:4] == 4)
    assert np.all(result.vertical_smoothing_support[5:] == 4)


def test_nonidentity_world_transform_preserves_normalized_projection_and_loader(tmp_path):
    raw = {
        "fps": 30.0, "width": 1920, "height": 1080,
        "intrinsics": [{"width": 1920, "height": 1080, "params": [1000.0]}],
        "poses": [
            {"frame_index": 0, "registered": True, "center": [1.0, 2.0, 3.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
            {"frame_index": 10, "registered": True, "center": [2.0, 2.0, 3.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
        ],
    }
    rotation = _rotz(35.0)
    transform = solve_rank1_transform(
        _trajectory(np.asarray([[1.0, 2.0, 3.0], [2.0, 2.0, 3.0]])),
        _scale_fit(2.25),
        np.asarray([1.0, 0.0, 0.0]),
        [_prior(0, rotation, 2.25 * (rotation @ np.asarray([1.0, 2.0, 3.0])) + np.asarray([4.0, -2.0, 1.0]))],
        Rank1Config(min_baseline=0.1),
    )
    centers = transform.apply_points(np.asarray([pose["center"] for pose in raw["poses"]]))
    output = build_rank1_trajectory_json(raw, transform, centers)

    old_center = np.asarray(raw["poses"][0]["center"])
    old_point = np.asarray([4.0, 6.0, 12.0])
    new_center = np.asarray(output["poses"][0]["center"])
    new_point = transform.apply_points(np.asarray([old_point]))[0]
    old_camera = quat_wxyz_to_matrix(raw["poses"][0]["cam_from_world_quat_wxyz"]) @ (old_point - old_center)
    new_camera = quat_wxyz_to_matrix(output["poses"][0]["cam_from_world_quat_wxyz"]) @ (new_point - new_center)
    assert np.allclose(old_camera[:2] / old_camera[2], new_camera[:2] / new_camera[2])
    assert output["meta"]["coordinate_system"] == "cad_meters"
    assert output["meta"]["trajectory_mode"] == "srt_rank1_manual_prior"

    path = tmp_path / "rank1.json"
    path.write_text(__import__("json").dumps(output), encoding="utf-8")
    loaded = load_sfm_trajectory(path)
    assert np.allclose(loaded.centers, centers)
