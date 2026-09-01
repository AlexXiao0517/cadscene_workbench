"""Camera intrinsics and DJI absolute-gimbal pose conversion."""

from __future__ import annotations

from math import isfinite, radians, tan

from cadscene.core.camera import (
    CameraState,
    camera_to_world_rotation,
    rotation_matrix_to_quaternion_wxyz,
)


DJI_ABSOLUTE_NED_PROFILE = "dji_absolute_ned"


def horizontal_fov_intrinsics(
    width: int, height: int, horizontal_fov_deg: float
) -> dict[str, object]:
    image_width = int(width)
    image_height = int(height)
    fov = float(horizontal_fov_deg)
    if image_width <= 0 or image_height <= 0:
        raise ValueError("video width and height must be positive")
    if not isfinite(fov) or not 1.0 < fov < 179.0:
        raise ValueError("horizontal_fov_deg must be finite and inside (1, 179)")
    focal = (image_width * 0.5) / tan(radians(fov) * 0.5)
    return {
        "model": "PINHOLE",
        "width": image_width,
        "height": image_height,
        "params": [
            float(focal),
            float(focal),
            image_width * 0.5,
            image_height * 0.5,
        ],
        "horizontal_fov_deg": fov,
        "fov_source": "user",
    }


def dji_ned_gimbal_to_cam_from_world_quat(
    yaw: float,
    pitch: float,
    roll: float,
    *,
    profile: str,
) -> list[float]:
    if profile != DJI_ABSOLUTE_NED_PROFILE:
        raise ValueError(
            f"unsupported DJI attitude profile: {profile!r}; "
            f"expected {DJI_ABSOLUTE_NED_PROFILE!r}"
        )
    angles = (float(yaw), float(pitch), float(roll))
    if not all(isfinite(value) for value in angles):
        raise ValueError("DJI gimbal yaw/pitch/roll must be finite")
    state = CameraState(
        yaw_deg=angles[0],
        # DJI geographic NED gimbal pitch is negative when pointing down;
        # CameraState pitch is positive when its forward axis points down.
        pitch_deg=-angles[1],
        roll_deg=angles[2],
    )
    camera_to_world = camera_to_world_rotation(state)
    return rotation_matrix_to_quaternion_wxyz(camera_to_world.T)
