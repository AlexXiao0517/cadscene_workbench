from __future__ import annotations

import pytest

from cadscene.pure_rotation.raw_trajectory import convert_poc_raw_trajectory


def test_poc_world_from_camera_is_preserved_as_local_from_camera_and_xyzw_becomes_wxyz() -> None:
    poc = {
        "trajectory_mode": "pure_rotation_only",
        "translation_observable": False,
        "absolute_orientation_observable": False,
        "camera_center_mode": "fixed",
        "coordinate_system": "local_rotation_segments",
        "orientation_source": "opengv_rotation_only",
        "intrinsics_source": "unverified_same_resolution_candidate",
        "intrinsics_verified": False,
        "poses": [{"decoded_frame_index": 7, "segment_id": 2, "rotation_world_from_camera": [[0, -1, 0], [1, 0, 0], [0, 0, 1]], "camera_center": [0, 0, 0], "quaternion_xyzw": [0, 0, 0.7071067811865476, 0.7071067811865476]}],
    }

    converted = convert_poc_raw_trajectory(poc, pts_by_decoded_index={7: 1.25})

    pose = converted["poses"][0]
    assert pose["rotation_local_from_camera"] == poc["poses"][0]["rotation_world_from_camera"]
    assert pose["quaternion_wxyz"] == [0.7071067811865476, 0, 0, 0.7071067811865476]
    assert pose["camera_center_local"] == [0.0, 0.0, 0.0]
    assert pose["pts_time_sec"] == 1.25


def test_raw_conversion_rejects_nonzero_center_and_invalid_rotation() -> None:
    bad = {"poses": [{"decoded_frame_index": 0, "segment_id": 0, "rotation_world_from_camera": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "camera_center": [1, 0, 0]}]}

    with pytest.raises(ValueError, match="camera center"):
        convert_poc_raw_trajectory(bad, pts_by_decoded_index={0: 0.0})
