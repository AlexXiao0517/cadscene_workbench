from __future__ import annotations

import numpy as np
import pytest

from cadscene.core.camera import quaternion_wxyz_to_rotation_matrix
from cadscene.srt.full_pose import (
    dji_ned_gimbal_to_cam_from_world_quat,
    horizontal_fov_intrinsics,
)


def _camera_to_world_from_quaternion(quaternion: list[float]) -> np.ndarray:
    return quaternion_wxyz_to_rotation_matrix(quaternion).T


def test_horizontal_fov_produces_square_pixel_pinhole_intrinsics() -> None:
    intrinsics = horizontal_fov_intrinsics(3840, 2160, 90.0)

    assert intrinsics["model"] == "PINHOLE"
    assert intrinsics["width"] == 3840
    assert intrinsics["height"] == 2160
    assert intrinsics["params"] == pytest.approx(
        [1920.0, 1920.0, 1920.0, 1080.0]
    )
    assert intrinsics["horizontal_fov_deg"] == 90.0


@pytest.mark.parametrize("bad_fov", [float("nan"), 1.0, 179.0])
def test_horizontal_fov_rejects_non_finite_or_boundary_values(
    bad_fov: float,
) -> None:
    with pytest.raises(ValueError, match="horizontal_fov_deg"):
        horizontal_fov_intrinsics(3840, 2160, bad_fov)


@pytest.mark.parametrize(
    ("yaw", "expected_forward"),
    [(0.0, [0.0, 1.0, 0.0]), (90.0, [1.0, 0.0, 0.0])],
)
def test_dji_yaw_zero_is_north_and_positive_clockwise(
    yaw: float, expected_forward: list[float]
) -> None:
    quaternion = dji_ned_gimbal_to_cam_from_world_quat(
        yaw,
        0.0,
        0.0,
        profile="dji_absolute_ned",
    )
    camera_to_world = _camera_to_world_from_quaternion(quaternion)

    assert camera_to_world[:, 2] == pytest.approx(expected_forward, abs=1e-9)


def test_negative_dji_pitch_points_camera_downward() -> None:
    quaternion = dji_ned_gimbal_to_cam_from_world_quat(
        0.0,
        -45.0,
        0.0,
        profile="dji_absolute_ned",
    )
    camera_to_world = _camera_to_world_from_quaternion(quaternion)

    assert camera_to_world[2, 2] == pytest.approx(-2**-0.5, abs=1e-9)


def test_dji_roll_survives_camera_state_round_trip() -> None:
    quaternion = dji_ned_gimbal_to_cam_from_world_quat(
        30.0,
        -20.0,
        7.0,
        profile="dji_absolute_ned",
    )
    camera_to_world = _camera_to_world_from_quaternion(quaternion)

    from cadscene.core.camera import decompose_world_from_camera_rotation

    yaw, pitch, roll = decompose_world_from_camera_rotation(camera_to_world)
    assert yaw == pytest.approx(30.0, abs=1e-9)
    assert pitch == pytest.approx(20.0, abs=1e-9)
    assert roll == pytest.approx(7.0, abs=1e-9)


def test_dji_pose_requires_explicit_supported_attitude_profile() -> None:
    with pytest.raises(ValueError, match="profile"):
        dji_ned_gimbal_to_cam_from_world_quat(
            0.0,
            0.0,
            0.0,
            profile="relative_to_aircraft",
        )
