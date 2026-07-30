from __future__ import annotations

import numpy as np
import pytest

from cadscene.srt.fusion import (
    build_fused_trajectory_json,
    estimate_sfm_to_srt_sim3,
    fuse_positions,
    quat_wxyz_to_matrix,
    rotate_cam_from_world_quat,
    select_constraint_indices,
)
from cadscene.srt.quality import FusionConfig


def test_known_sim3_is_recovered_with_fixed_seed_and_gps_outlier_is_rejected() -> None:
    source = np.array(
        [[0.0, 0.0, 0.0], [2.0, 0.0, 0.5], [0.0, 3.0, 0.2], [2.0, 3.0, 1.0], [1.0, 1.5, 0.4], [4.0, 1.0, 0.6]]
    )
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    target = 2.5 * (source @ rotation.T) + np.array([10.0, -4.0, 8.0])
    target[-1] += np.array([50.0, -50.0, 20.0])

    fit = estimate_sfm_to_srt_sim3(
        source,
        target,
        FusionConfig(min_common_frames=5, min_baseline_m=3.0, ransac_seed=7, ransac_iterations=200),
    )

    assert fit.scale == pytest.approx(2.5, rel=1e-5)
    assert fit.inlier_ratio > 0.8
    assert fit.inlier_mask[-1] is False
    assert fit.rmse_m < 1e-5


def test_constraint_selection_spatially_deduplicates_repeated_gps() -> None:
    points = np.array([[0.0, 0.0, 0.0], [0.01, 0.0, 0.0], [0.02, 0.0, 0.0], [2.0, 0.0, 0.0], [4.0, 1.0, 0.0]])

    selected = select_constraint_indices(points, FusionConfig(spatial_dedupe_m=0.5))

    assert selected.tolist() == [0, 3, 4]


def test_srt_jump_is_not_copied_to_fused_position() -> None:
    metric = np.column_stack([np.arange(7, dtype=float), np.zeros(7), np.zeros(7)])
    srt = metric.copy()
    srt[3, 0] += 30.0

    result = fuse_positions(
        metric,
        frame_times_sec=np.arange(7, dtype=float),
        srt_positions=srt,
        srt_valid=np.ones(7, dtype=bool),
        smoothing_window_sec=2.0,
        min_smoothing_support=3,
    )

    assert result.fused_positions[3, 0] == pytest.approx(metric[3, 0])


def test_non_identity_sim3_rotation_preserves_normalized_projection() -> None:
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    old_rotation = np.eye(3)
    old_quat = np.array([1.0, 0.0, 0.0, 0.0])
    old_point = np.array([1.0, 2.0, 5.0])
    old_center = np.array([0.0, 0.0, 0.0])
    scale = 2.5
    translation = np.array([10.0, -3.0, 2.0])

    new_quat = rotate_cam_from_world_quat(old_quat, rotation)
    new_point = scale * rotation @ old_point + translation
    new_center = scale * rotation @ old_center + translation
    old_camera = old_rotation @ (old_point - old_center)
    new_camera = quat_wxyz_to_matrix(new_quat) @ (new_point - new_center)

    assert new_camera[0] / new_camera[2] == pytest.approx(old_camera[0] / old_camera[2])
    assert new_camera[1] / new_camera[2] == pytest.approx(old_camera[1] / old_camera[2])


def test_fused_trajectory_retains_loader_compatible_pose_fields() -> None:
    raw = {
        "fps": 25.0,
        "width": 1920,
        "height": 1080,
        "intrinsics": [{"params": [1000.0], "width": 1920}],
        "poses": [{"frame_index": 1, "registered": True, "center": [0.0, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]}],
    }
    result = fuse_positions(
        np.array([[0.0, 0.0, 0.0]]),
        frame_times_sec=[0.0],
        srt_positions=np.array([[1.0, 0.0, 0.0]]),
        srt_valid=[True],
        min_smoothing_support=1,
    )

    fused = build_fused_trajectory_json(raw, result, sim3_rotation=np.eye(3), coordinate_meta={"up_source": "rel_alt_relative"})

    assert fused["poses"][0]["center"] == [1.0, 0.0, 0.0]
    assert fused["poses"][0]["cam_from_world_quat_wxyz"] == [1.0, 0.0, 0.0, 0.0]
    assert fused["meta"]["orientation_source"] == "sfm"
