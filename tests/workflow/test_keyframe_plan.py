from __future__ import annotations

from pathlib import Path

from cadscene.core.io import write_json
import pytest

from cadscene.workflow.keyframe_plan import (
    create_keyframe_plan,
    keyframe_plan_progress_changed,
    sync_keyframe_plan,
    validate_quality_plan,
)


def _trajectory(path: Path) -> None:
    write_json(
        path,
        {
            "fps": 25.0,
            "poses": [
                {
                    "frame_index": frame,
                    "registered": True,
                    "center": [float(frame), 0.0, 0.0],
                    "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                }
                for frame in (0, 120, 240, 300)
            ],
        },
    )


def _track(frames: list[int]) -> dict:
    return {
        "keyframes": [
            {
                "frame": frame,
                "source": "manual_keyframe",
                "camera": {"x": 0, "y": 0, "z": 100, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70},
            }
            for frame in frames
        ]
    }


def test_keyframe_plan_uses_first_anchor_interval_and_last_sfm_frame(tmp_path: Path) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    _trajectory(trajectory)

    plan = create_keyframe_plan(trajectory, _track([0, 122]), interval_frames=120)

    assert plan["interval_frames"] == 120
    assert plan["start_frame"] == 0
    assert plan["end_frame"] == 300
    assert [item["frame_index"] for item in plan["frames"]] == [0, 120, 240, 300]
    assert plan["frames"][0]["status"] == "completed"
    assert plan["frames"][1]["status"] == "pending"


def test_keyframe_plan_rejects_unconfirmed_initial_seed(tmp_path: Path) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    _trajectory(trajectory)
    unconfirmed = {
        "keyframes": [
            {
                "frame": 0,
                "camera": {"x": 0, "y": 0, "z": 100, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70},
            }
        ]
    }

    with pytest.raises(ValueError, match="create a manual keyframe"):
        create_keyframe_plan(trajectory, unconfirmed, interval_frames=120)


def test_keyframe_plan_sync_marks_only_matching_manual_frames_complete(tmp_path: Path) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    _trajectory(trajectory)
    plan = create_keyframe_plan(trajectory, _track([0]), interval_frames=120)

    synced = sync_keyframe_plan(plan, _track([0, 120, 122]))

    assert [item["status"] for item in synced["frames"]] == ["completed", "completed", "pending", "pending"]
    assert synced["completed_count"] == 2
    assert synced["pending_count"] == 2


def test_keyframe_plan_progress_only_changes_when_a_planned_frame_is_confirmed(tmp_path: Path) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    _trajectory(trajectory)
    plan = create_keyframe_plan(trajectory, _track([0]), interval_frames=120)

    unchanged = sync_keyframe_plan(plan, _track([0, 122]))
    changed = sync_keyframe_plan(plan, _track([0, 120]))

    assert not keyframe_plan_progress_changed(plan, unchanged)
    assert keyframe_plan_progress_changed(plan, changed)


def test_quality_requires_completed_plan_and_a_later_route_fit(tmp_path: Path) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    _trajectory(trajectory)
    plan_path = tmp_path / "keyframe_plan.json"
    plan_path.write_text(__import__("json").dumps(create_keyframe_plan(trajectory, _track([0]), interval_frames=120)), encoding="utf-8")
    aligned_path = tmp_path / "sfm_camera_path.csv"
    aligned_path.write_text("frame_index\n0\n", encoding="utf-8")

    with pytest.raises(ValueError, match="complete planned keyframes"):
        validate_quality_plan(plan_path, aligned_path)


def test_quality_requires_a_keyframe_plan_in_the_guided_workflow(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="generate a keyframe plan"):
        validate_quality_plan(tmp_path / "missing_plan.json", tmp_path / "sfm_camera_path.csv")
