from __future__ import annotations

import numpy as np

from cadscene.pure_rotation.placement import apply_global_placement


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
