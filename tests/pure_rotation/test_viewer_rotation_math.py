from __future__ import annotations

import json
import subprocess
from pathlib import Path


MODULE = Path("apps/web_camera_viewer/pure_rotation_math.js")


def _node(expression: str):
    script = f"const m=require({json.dumps(str(MODULE.resolve()))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_identity_camera_axes_match_viewer_zero_yaw_pitch_roll() -> None:
    # Viewer zero orientation: right=-X, down=-Z, forward=+Y.
    matrix = [[-1, 0, 0], [0, 0, 1], [0, -1, 0]]
    result = _node(f"m.matrixToViewerEuler({json.dumps(matrix)})")
    assert abs(result["yaw"]) < 1e-9
    assert abs(result["pitch"]) < 1e-9
    assert abs(result["roll"]) < 1e-9


def test_pts_interpolation_uses_rotation_slerp_and_fixed_center() -> None:
    poses = [
        {"pts_time_sec": 0, "segment_id": 0, "rotation_cad_from_camera": [[-1,0,0],[0,0,1],[0,-1,0]], "camera_center_web": [1,2,3]},
        {"pts_time_sec": 2, "segment_id": 0, "rotation_cad_from_camera": [[0,-1,0],[-1,0,0],[0,0,-1]], "camera_center_web": [1,2,3]},
    ]
    result = _node(f"m.poseAtPts({json.dumps(poses)}, 1)")
    assert result["camera_center_web"] == [1, 2, 3]
    assert result["segment_id"] == 0


def test_viewer_euler_round_trip_produces_same_orientation() -> None:
    result = _node("m.matrixToViewerEuler(m.viewerEulerToMatrix({yaw:35,pitch:-20,roll:12}))")
    assert abs(result["yaw"] - 35) < 1e-9
    assert abs(result["pitch"] + 20) < 1e-9
    assert abs(result["roll"] - 12) < 1e-9


def test_raw_local_identity_is_displayed_as_viewer_zero_orientation() -> None:
    result = _node("m.matrixToViewerEuler(m.localRotationToViewerMatrix([[1,0,0],[0,1,0],[0,0,1]]))")
    assert all(abs(result[key]) < 1e-9 for key in ("yaw", "pitch", "roll"))
