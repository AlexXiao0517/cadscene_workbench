from __future__ import annotations

import numpy as np

from cadscene.pure_rotation.placement import apply_global_placement
from cadscene.pure_rotation.rotation_matrix import coerce_so3_matrix, normalize_rotation_fields


def test_anchor_formula_matches_manual_orientation_and_keeps_center_fixed() -> None:
    raw = {"poses": [
        {"decoded_frame_index": 0, "pts_time_sec": 0.0, "segment_id": 0, "rotation_local_from_camera": [[1,0,0],[0,1,0],[0,0,1]]},
        {"decoded_frame_index": 1, "pts_time_sec": 1.0, "segment_id": 0, "rotation_local_from_camera": [[0,-1,0],[1,0,0],[0,0,1]]},
    ]}
    manual_anchor = [[0,-1,0],[1,0,0],[0,0,1]]

    track = apply_global_placement(raw, segment_id=0, anchor_decoded_frame_index=0, camera_center_web=[1,2,3], manual_rotation_cad_from_camera=manual_anchor, fov=60)

    poses = track["poses"]
    assert np.allclose(poses[0]["rotation_cad_from_camera"], manual_anchor)
    assert poses[0]["camera_center_web"] == [1.0, 2.0, 3.0]
    assert poses[1]["camera_center_web"] == [1.0, 2.0, 3.0]
    assert np.allclose(poses[1]["rotation_cam_from_cad"], np.asarray(poses[1]["rotation_cad_from_camera"]).T)


def test_legacy_viewer_reflection_is_migrated_by_flipping_camera_right_axis() -> None:
    legacy_zero = [[-1, 0, 0], [0, 0, 1], [0, -1, 0]]

    migrated = coerce_so3_matrix(legacy_zero, allow_legacy_reflection=True)

    assert np.allclose(migrated, [[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    assert np.isclose(np.linalg.det(migrated), 1.0)


def test_global_placement_always_emits_proper_rotations() -> None:
    raw = {"poses": [
        {"decoded_frame_index": 0, "pts_time_sec": 0.0, "segment_id": 0, "rotation_local_from_camera": np.eye(3).tolist()},
        {"decoded_frame_index": 1, "pts_time_sec": 1.0, "segment_id": 0, "rotation_local_from_camera": [[0,-1,0],[1,0,0],[0,0,1]]},
    ]}
    legacy_zero = [[-1, 0, 0], [0, 0, 1], [0, -1, 0]]

    track = apply_global_placement(
        raw,
        segment_id=0,
        anchor_decoded_frame_index=0,
        camera_center_web=[1, 2, 3],
        manual_rotation_cad_from_camera=legacy_zero,
        fov=60,
    )

    assert all(np.isclose(np.linalg.det(pose["rotation_cad_from_camera"]), 1.0) for pose in track["poses"])


def test_saved_payload_rotation_fields_are_normalized_without_mutating_input() -> None:
    legacy_zero = [[-1, 0, 0], [0, 0, 1], [0, -1, 0]]
    payload = {"manual_rotation_cad_from_camera": legacy_zero, "note": "keep"}

    normalized = normalize_rotation_fields(
        payload,
        ("manual_rotation_cad_from_camera",),
        allow_legacy_reflection=True,
    )

    assert np.isclose(np.linalg.det(normalized["manual_rotation_cad_from_camera"]), 1.0)
    assert payload["manual_rotation_cad_from_camera"] == legacy_zero
    assert normalized["note"] == "keep"
