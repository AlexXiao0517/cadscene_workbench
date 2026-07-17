from cadscene.core.camera import CameraState
from cadscene.core.coordinates import (
    cad_meters_to_web_camera,
    python_state_to_web_camera,
    web_camera_to_cad_meters,
    web_camera_to_python_state,
)


def test_web_camera_to_cad_meters_uses_origin_scale_and_pitch_sign():
    camera = {
        "x": 110.0,
        "y": 220.0,
        "z": 30.0,
        "yaw": 12.0,
        "pitch": -45.0,
        "roll": 3.0,
        "fov": 70.0,
    }

    state = web_camera_to_python_state(camera, origin_xy=(100.0, 200.0), cad_scale=0.5)

    assert state == CameraState(
        camera_x=5.0,
        camera_y=10.0,
        camera_z=15.0,
        yaw_deg=12.0,
        pitch_deg=45.0,
        roll_deg=3.0,
        fov_deg=70.0,
        cad_scale=0.5,
    )


def test_python_state_to_web_camera_roundtrips_core_fields():
    state = CameraState(
        camera_x=5.0,
        camera_y=10.0,
        camera_z=15.0,
        yaw_deg=12.0,
        pitch_deg=45.0,
        roll_deg=3.0,
        fov_deg=70.0,
        cad_scale=0.5,
    )

    camera = python_state_to_web_camera(state, origin_xy=(100.0, 200.0))

    assert camera == {
        "x": 110.0,
        "y": 220.0,
        "z": 30.0,
        "yaw": 12.0,
        "pitch": -45.0,
        "roll": 3.0,
        "fov": 70.0,
    }


def test_cad_meters_to_web_camera_accepts_explicit_scale():
    assert cad_meters_to_web_camera((5.0, 10.0, 15.0), origin_xy=(100.0, 200.0), cad_scale=0.5) == {
        "x": 110.0,
        "y": 220.0,
        "z": 30.0,
    }
    assert web_camera_to_cad_meters({"x": 110.0, "y": 220.0, "z": 30.0}, (100.0, 200.0), 0.5) == (
        5.0,
        10.0,
        15.0,
    )

