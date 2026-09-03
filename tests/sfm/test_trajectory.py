from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cadscene.sfm.trajectory import SfmTrajectory, load_sfm_trajectory


def test_sfm_trajectory_query_interpolates_center() -> None:
    traj = SfmTrajectory(
        frames=np.asarray([0, 10], dtype=np.int64),
        centers=np.asarray([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]], dtype=np.float64),
        quats_c2w_wxyz=np.asarray([[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]], dtype=np.float64),
        fps=25.0,
        width=100,
        height=100,
        intrinsics={"width": 100, "params": [50.0]},
    )

    center, rotation = traj.query(5)

    np.testing.assert_allclose(center, [5.0, 0.0, 0.0])
    np.testing.assert_allclose(rotation, np.eye(3))


def test_full_pose_loader_preserves_explicit_unregistered_frames(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text(
        json.dumps(
            {
                "poses": [
                    {
                        "frame_index": 0,
                        "registered": True,
                        "center": [0.0, 0.0, 0.0],
                        "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    {
                        "frame_index": 1,
                        "registered": False,
                        "center": [0.0, 0.0, 0.0],
                        "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                    {
                        "frame_index": 2,
                        "registered": True,
                        "center": [2.0, 0.0, 0.0],
                        "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                    },
                ],
                "meta": {
                    "trajectory_mode": "srt_full_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                },
            }
        ),
        encoding="utf-8",
    )

    trajectory = load_sfm_trajectory(path)

    assert trajectory.is_frame_registered(0)
    assert not trajectory.is_frame_registered(1)
    assert trajectory.is_frame_registered(2)
    with pytest.raises(ValueError, match="explicitly unregistered"):
        trajectory.query(1)
    with pytest.raises(ValueError, match="crosses an unregistered gap"):
        trajectory.query(1.5)


def test_fixed_track_loader_keeps_positions_without_available_orientation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixed-track.json"
    path.write_text(
        json.dumps(
            {
                "fps": 25.0,
                "width": 1920,
                "height": 1080,
                "intrinsics": [
                    {"model": "PINHOLE", "width": 1920, "params": [960, 960, 960, 540]}
                ],
                "poses": [
                    {
                        "frame_index": index,
                        "registered": False,
                        "position_available": True,
                        "orientation_available": False,
                        "center": [10.0 + index, 20.0, 80.0 + index],
                    }
                    for index in range(3)
                ],
                "meta": {
                    "trajectory_mode": "srt_fixed_track_visual_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                    "position_source": "srt_cad_locked",
                },
            }
        ),
        encoding="utf-8",
    )

    trajectory = load_sfm_trajectory(path)

    assert trajectory.position_frames.tolist() == [0, 1, 2]
    np.testing.assert_allclose(trajectory.center_at(1), [11.0, 20.0, 81.0])
    np.testing.assert_allclose(trajectory.center_at(1.5), [11.5, 20.0, 81.5])
    assert trajectory.is_orientation_available(1) is False
    with pytest.raises(ValueError, match="orientation"):
        trajectory.orientation_at(1)
