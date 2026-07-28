from __future__ import annotations

import numpy as np

from cadscene.pure_rotation.corrections import apply_rotation_corrections


def test_correction_residual_is_slerped_not_euler_interpolated() -> None:
    identity = np.eye(3).tolist()
    base = {"poses": [{"decoded_frame_index": index, "segment_id": 0, "rotation_cad_from_camera": identity, "camera_center_web": [1,2,3]} for index in (0, 5, 10)]}
    yaw90 = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    corrections = [
        {"decoded_frame_index": 0, "segment_id": 0, "manual_rotation_cad_from_camera": identity},
        {"decoded_frame_index": 10, "segment_id": 0, "manual_rotation_cad_from_camera": yaw90},
    ]

    result = apply_rotation_corrections(base, corrections)

    middle = np.asarray(result["poses"][1]["rotation_cad_from_camera"])
    expected = np.array([[2**-0.5, -2**-0.5, 0], [2**-0.5, 2**-0.5, 0], [0,0,1]])
    assert np.allclose(middle, expected, atol=1e-6)
    assert result["poses"][1]["camera_center_web"] == [1, 2, 3]


def test_legacy_reflection_correction_is_migrated_before_slerp() -> None:
    identity = np.eye(3).tolist()
    base = {"poses": [
        {"decoded_frame_index": index, "segment_id": 0, "rotation_cad_from_camera": identity, "camera_center_web": [1,2,3]}
        for index in (0, 1, 2)
    ]}
    legacy_zero = [[-1, 0, 0], [0, 0, 1], [0, -1, 0]]
    corrections = [
        {"decoded_frame_index": 0, "segment_id": 0, "manual_rotation_cad_from_camera": legacy_zero},
        {"decoded_frame_index": 2, "segment_id": 0, "manual_rotation_cad_from_camera": identity},
    ]

    result = apply_rotation_corrections(base, corrections)

    assert all(np.isclose(np.linalg.det(pose["rotation_cad_from_camera"]), 1.0) for pose in result["poses"])
