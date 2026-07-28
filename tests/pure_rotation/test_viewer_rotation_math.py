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
    # Authoritative camera frame is right-handed: right=+X, down=-Z, forward=+Y.
    matrix = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
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


def test_viewer_euler_matrix_is_a_proper_so3_rotation() -> None:
    result = _node(
        "(()=>{const a=m.viewerEulerToMatrix({yaw:35,pitch:-20,roll:12});"
        "return a[0][0]*(a[1][1]*a[2][2]-a[1][2]*a[2][1])"
        "-a[0][1]*(a[1][0]*a[2][2]-a[1][2]*a[2][0])"
        "+a[0][2]*(a[1][0]*a[2][1]-a[1][1]*a[2][0]);})()"
    )
    assert abs(result - 1.0) < 1e-9


def test_raw_local_identity_is_displayed_as_viewer_zero_orientation() -> None:
    result = _node("m.matrixToViewerEuler(m.localRotationToViewerMatrix([[1,0,0],[0,1,0],[0,0,1]]))")
    assert all(abs(result[key]) < 1e-9 for key in ("yaw", "pitch", "roll"))


def test_draft_anchor_matches_manual_orientation_at_current_pts() -> None:
    anchor_local = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    manual = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    result = _node(
        f"m.applyDraftPlacement({json.dumps(anchor_local)}, "
        f"{json.dumps(anchor_local)}, {json.dumps(manual)})"
    )
    assert result == manual


def test_draft_anchor_propagates_relative_rotation_from_anchor_time() -> None:
    identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    yaw90 = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    yaw180 = [[-1, 0, 0], [0, -1, 0], [0, 0, 1]]
    result = _node(
        f"m.applyDraftPlacement({json.dumps(yaw180)}, "
        f"{json.dumps(yaw90)}, {json.dumps(identity)})"
    )
    assert result == yaw90


def test_candidate_intrinsics_are_converted_to_horizontal_display_fov() -> None:
    result = _node("m.horizontalFovDeg(1920, 2218.6501101684794)")
    assert 46 < result < 48


def test_euler_display_wrap_uses_nearest_equivalent_angle() -> None:
    assert _node("m.unwrapDegreesNear(-179.5, 179.5)") == 180.5
    assert _node("m.unwrapDegreesNear(179.5, -179.5)") == -180.5


def test_world_vertical_rotation_left_multiplies_a_tilted_camera() -> None:
    tilted = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]
    result = _node(f"m.rotateAboutWorldUp({json.dumps(tilted)}, 90)")
    expected = [[0, 0, 1], [1, 0, 0], [0, 1, 0]]
    for actual_row, expected_row in zip(result, expected, strict=True):
        assert all(abs(actual - wanted) < 1e-9 for actual, wanted in zip(actual_row, expected_row, strict=True))


def test_world_vertical_slider_values_are_relative_to_the_same_baseline() -> None:
    result = _node(
        "(()=>{const base=[[1,0,0],[0,1,0],[0,0,1]];"
        "return [m.rotateAboutWorldUp(base,10),m.rotateAboutWorldUp(base,20)];})()"
    )
    ten_degrees, twenty_degrees = result
    assert abs(ten_degrees[0][0] - 0.984807753012208) < 1e-9
    assert abs(twenty_degrees[0][0] - 0.9396926207859084) < 1e-9
    assert abs(twenty_degrees[0][0] - ten_degrees[0][0]) > 0.01
