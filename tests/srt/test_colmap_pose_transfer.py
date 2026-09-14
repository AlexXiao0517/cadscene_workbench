from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation, Slerp

from cadscene.core.camera import rotation_matrix_to_quaternion_wxyz
from cadscene.core.sim3 import Sim3
from cadscene.sfm.trajectory import SfmTrajectory
from cadscene.srt.colmap_pose_transfer import (
    ColmapPoseTransferError,
    orientation_solution_from_colmap_transfer,
    transfer_colmap_pose_to_srt,
)
from cadscene.srt.fixed_track_visual_pose import FixedTrackPosition


def _position(frame: int, center: np.ndarray) -> FixedTrackPosition:
    values = tuple(float(value) for value in center)
    return FixedTrackPosition(
        frame_index=frame,
        source_pts=frame,
        pts_time_sec=frame / 10.0,
        canonical_center=values,
        center=values,
        latitude=30.0,
        longitude=120.0,
        rel_alt=values[2],
        abs_alt=None,
        projected_easting=values[0],
        projected_northing=values[1],
        cad_raw_x=values[0],
        cad_raw_y=values[1],
        interpolated=False,
        source_entry_before=frame,
        source_entry_after=frame,
    )


def _synthetic_case() -> tuple[SfmTrajectory, list[FixedTrackPosition], Sim3, dict[int, np.ndarray]]:
    frames = np.asarray([0, 10, 20, 30, 40], dtype=np.int64)
    source_centers = np.asarray(
        [
            [0.0, 0.0, 0.0],
            [3.0, 0.0, 0.2],
            [5.0, 2.0, 0.5],
            [7.0, 3.0, 0.4],
            [8.0, 6.0, 0.8],
        ],
        dtype=np.float64,
    )
    mapping = Sim3(
        scale=2.5,
        rotation=Rotation.from_euler("zyx", [25.0, -8.0, 4.0], degrees=True).as_matrix(),
        translation=np.asarray([100.0, -20.0, 80.0]),
    )
    source_world_from_camera = {
        int(frame): Rotation.from_euler(
            "zyx", [float(frame), -20.0, 2.0], degrees=True
        ).as_matrix()
        for frame in frames
    }
    trajectory = SfmTrajectory(
        frames=frames,
        centers=source_centers,
        quats_c2w_wxyz=np.asarray(
            [
                rotation_matrix_to_quaternion_wxyz(
                    source_world_from_camera[int(frame)].T
                )
                for frame in frames
            ],
            dtype=np.float64,
        ),
        fps=10.0,
        width=1920,
        height=1080,
        intrinsics={"model": "PINHOLE", "width": 1920, "params": [1000.0]},
    )
    positions = [
        _position(int(frame), center)
        for frame, center in zip(frames, mapping.apply(source_centers))
    ]
    return trajectory, positions, mapping, source_world_from_camera


def _long_route_case(
    *, deformation_m: float,
) -> tuple[SfmTrajectory, list[FixedTrackPosition]]:
    frames = np.arange(101, dtype=np.int64) * 10
    progress = np.linspace(0.0, 1.0, len(frames))
    source_centers = np.column_stack(
        (
            12.0 * progress,
            0.4 * np.sin(2.0 * np.pi * progress),
            0.05 * np.sin(4.0 * np.pi * progress),
        )
    )
    mapping = Sim3(
        scale=250.0,
        rotation=Rotation.from_euler("z", 18.0, degrees=True).as_matrix(),
        translation=np.asarray([500.0, 800.0, 90.0]),
    )
    target_centers = mapping.apply(source_centers)
    target_centers[:, 1] += deformation_m * np.sin(6.0 * np.pi * progress)
    world_from_camera = Rotation.from_euler(
        "zyx",
        np.column_stack(
            (
                30.0 * progress,
                np.full(len(frames), -25.0),
                np.zeros(len(frames)),
            )
        ),
        degrees=True,
    ).as_matrix()
    trajectory = SfmTrajectory(
        frames=frames,
        centers=source_centers,
        quats_c2w_wxyz=np.asarray(
            [
                rotation_matrix_to_quaternion_wxyz(rotation.T)
                for rotation in world_from_camera
            ],
            dtype=np.float64,
        ),
        fps=10.0,
        width=1920,
        height=1080,
        intrinsics={"model": "PINHOLE", "width": 1920, "params": [1000.0]},
    )
    return trajectory, [
        _position(int(frame), center)
        for frame, center in zip(frames, target_centers)
    ]


def test_transfer_uses_colmap_rotation_but_exact_srt_centers() -> None:
    trajectory, positions, mapping, source_rotations = _synthetic_case()

    result = transfer_colmap_pose_to_srt(trajectory, positions)

    assert result.registered_frames == (0, 10, 20, 30, 40)
    assert result.sim3.scale == pytest.approx(mapping.scale)
    np.testing.assert_allclose(result.sim3.rotation, mapping.rotation, atol=1e-8)
    for position in positions:
        np.testing.assert_array_equal(
            result.centers[position.frame_index],
            np.asarray(position.center, dtype=np.float64),
        )
        expected_world_from_camera = (
            mapping.rotation @ source_rotations[position.frame_index]
        )
        np.testing.assert_allclose(
            result.world_from_camera[position.frame_index],
            expected_world_from_camera,
            atol=1e-8,
        )


def test_transfer_scales_default_threshold_for_long_routes() -> None:
    trajectory, positions = _long_route_case(deformation_m=12.0)

    result = transfer_colmap_pose_to_srt(trajectory, positions)

    route_span_m = float(
        np.linalg.norm(
            np.ptp(np.asarray([position.center for position in positions]), axis=0)
        )
    )
    assert result.alignment_threshold_m == pytest.approx(route_span_m * 0.01)
    assert len(result.inlier_frames) >= len(positions) // 2
    for position in positions:
        np.testing.assert_array_equal(
            result.centers[position.frame_index],
            np.asarray(position.center, dtype=np.float64),
        )


def test_transfer_rejects_long_route_deformation_above_relative_threshold() -> None:
    trajectory, positions = _long_route_case(deformation_m=120.0)

    with pytest.raises(ColmapPoseTransferError, match="内点不足|残差超过门槛"):
        transfer_colmap_pose_to_srt(trajectory, positions)


def test_transfer_rejects_degenerate_center_geometry() -> None:
    trajectory, positions, _mapping, _source_rotations = _synthetic_case()
    degenerate = [
        _position(index * 10, np.asarray([float(index), 0.0, 0.0]))
        for index in range(5)
    ]

    with pytest.raises(ColmapPoseTransferError, match="退化"):
        transfer_colmap_pose_to_srt(trajectory, degenerate)


def test_orientation_solution_interpolates_short_registered_gaps() -> None:
    trajectory, positions, _mapping, _source_rotations = _synthetic_case()
    transfer = transfer_colmap_pose_to_srt(trajectory, positions)
    midpoint_center = 0.5 * (
        np.asarray(positions[0].center) + np.asarray(positions[1].center)
    )
    expanded = [positions[0], _position(5, midpoint_center), *positions[1:]]

    solution = orientation_solution_from_colmap_transfer(
        transfer,
        expanded,
        max_interpolation_gap_sec=2.0,
    )

    assert solution.status == "ready"
    assert set(solution.rotations) == {0, 5, 10, 20, 30, 40}
    expected_world_from_camera = Slerp(
        [0.0, 10.0],
        Rotation.from_matrix(
            [transfer.world_from_camera[0], transfer.world_from_camera[10]]
        ),
    )([5.0]).as_matrix()[0]
    actual_world_from_camera = solution.rotations[5].T
    np.testing.assert_allclose(
        actual_world_from_camera, expected_world_from_camera, atol=1e-8
    )
    assert solution.component_ids[5] == 0
    assert solution.diagnostics[0]["alignment_threshold_m"] == pytest.approx(5.0)
