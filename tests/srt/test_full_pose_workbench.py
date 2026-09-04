from __future__ import annotations

from copy import deepcopy

import pytest

from cadscene.srt.full_pose import dji_ned_gimbal_to_cam_from_world_quat
from cadscene.srt.full_pose_workbench import build_full_pose_workbench_payloads


def _trajectory() -> dict[str, object]:
    return {
        "fps": 25.0,
        "poses": [
            {
                "frame_index": 0,
                "source_pts": 100,
                "registered": True,
                "center": [10.0, 20.0, 30.0],
                "cam_from_world_quat_wxyz": (
                    dji_ned_gimbal_to_cam_from_world_quat(
                        12.0,
                        -35.0,
                        2.0,
                        profile="dji_absolute_ned",
                    )
                ),
            },
            {
                "frame_index": 1,
                "source_pts": 140,
                "registered": True,
                "center": [11.0, 21.0, 31.0],
                "cam_from_world_quat_wxyz": (
                    dji_ned_gimbal_to_cam_from_world_quat(
                        13.0,
                        -36.0,
                        3.0,
                        profile="dji_absolute_ned",
                    )
                ),
            },
        ],
        "meta": {
            "trajectory_mode": "srt_full_pose",
            "coordinate_system": "cad_local_m",
            "metric_scale_locked": True,
            "cad_origin_xy": [100.0, 200.0],
            "cad_scale": 1.0,
            "horizontal_fov_deg": 59.109,
        },
    }


def test_full_pose_workbench_payload_preserves_positions_attitudes_and_fov() -> None:
    trajectory = _trajectory()
    original = deepcopy(trajectory)

    payloads = build_full_pose_workbench_payloads(trajectory)

    keyframes = payloads.camera_track["keyframes"]
    assert [row["frame"] for row in keyframes] == [0, 1]
    assert all(row["source"] == "algorithm_prediction" for row in keyframes)
    assert all("position_locked" not in row for row in keyframes)
    assert keyframes[0]["time"] == pytest.approx(0.0)
    assert keyframes[0]["camera"] == pytest.approx(
        {
            "x": 110.0,
            "y": 220.0,
            "z": 30.0,
            "yaw": 12.0,
            "pitch": -35.0,
            "roll": 2.0,
            "fov": 59.109,
        }
    )
    assert payloads.camera_track["meta"] == {
        "generated_by": "cadscene.build_srt_full_pose",
        "coordinate_system": "web_cad_world",
        "workflow": "srt_full_pose",
        "pose_prior_schema": "srt_pose_prior_v1",
        "metric_scale_locked": True,
        "position_source": "srt_cad",
        "orientation_source": "srt_full_pose",
        "edit_policy": "six_dof_keyframe_residuals",
        "cad_scale": 1.0,
    }
    assert trajectory == original


def test_full_pose_workbench_scene_has_route_and_no_point_cloud() -> None:
    payloads = build_full_pose_workbench_payloads(_trajectory())

    assert payloads.viewer_scene["meta"]["workflow"] == "srt_full_pose"
    assert payloads.viewer_scene["meta"]["point_cloud_generated"] is False
    assert payloads.viewer_scene["points"]["count_original"] == 0
    assert payloads.viewer_scene["points"]["count_exported"] == 0
    route = payloads.viewer_scene["tracks"]["global_sfm_track"]
    assert [entry["frame_index"] for entry in route] == [0, 1]
    assert route[0]["camera"] == payloads.camera_track["keyframes"][0]["camera"]
    assert all("position_locked" not in entry for entry in route)


def test_full_pose_workbench_rejects_wrong_mode_and_empty_registered_route() -> None:
    wrong = _trajectory()
    wrong["meta"]["trajectory_mode"] = "sfm_only"
    with pytest.raises(ValueError, match="srt_full_pose"):
        build_full_pose_workbench_payloads(wrong)

    empty = _trajectory()
    for pose in empty["poses"]:
        pose["registered"] = False
    with pytest.raises(ValueError, match="registered poses"):
        build_full_pose_workbench_payloads(empty)
