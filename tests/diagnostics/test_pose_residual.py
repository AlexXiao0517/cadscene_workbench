from __future__ import annotations

from pathlib import Path

import numpy as np

from cadscene.core.io import write_csv_utf8_sig, write_json
from cadscene.diagnostics.pose_residual import (
    camera_z_profile,
    keyframe_pose_residuals,
    pose_compensation_warning,
)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    trajectory = tmp_path / "trajectory.json"
    write_json(
        trajectory,
        {
            "fps": 25,
            "poses": [
                {"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
                {"frame_index": 10, "registered": True, "center": [10, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
            ],
        },
    )
    alignment = tmp_path / "alignment.json"
    write_json(alignment, {"schema_version": "cadscene_alignment_v1", "sim3": {"scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation": [0, 0, 0]}})
    anchored = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(anchored, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 2, "yaw": 0, "pitch": 10, "roll": 0, "fov": 70}])
    track = tmp_path / "track.json"
    write_json(track, {"keyframes": [{"frame": 0, "source": "manual_keyframe", "camera": {"x": 0, "y": 0, "z": 2, "yaw": 0, "pitch": -10, "roll": 0, "fov": 70}}]})
    return trajectory, alignment, anchored, track


def test_keyframe_residual_uses_coordinate_conversion_and_pitch_sign(tmp_path: Path) -> None:
    trajectory, alignment, anchored, track = _inputs(tmp_path)

    rows = keyframe_pose_residuals(track, trajectory, alignment, anchored, cad_scale=1.0, origin_xy=(0.0, 0.0))

    assert rows[0]["manual_z"] == 2.0
    assert rows[0]["manual_vs_global_dz"] == 2.0
    assert rows[0]["manual_vs_anchored_dist"] == 0.0
    assert rows[0]["manual_vs_anchored_pitch"] == 0.0


def test_pose_compensation_warning_and_camera_z_profile(tmp_path: Path) -> None:
    trajectory, alignment, anchored, track = _inputs(tmp_path)
    residuals = keyframe_pose_residuals(track, trajectory, alignment, anchored, cad_scale=1.0, origin_xy=(0.0, 0.0))
    warning = pose_compensation_warning(residuals, [{"station_mid": 0.0, "median_z": 2.0}])
    z_rows = camera_z_profile(trajectory, alignment, anchored, track, cad_scale=1.0, origin_xy=(0.0, 0.0), profile_rows=[{"station_mid": 0, "median_z": 2.0}])

    assert warning["pose_compensation_warning"] is True
    assert "manual_z_if_available" in z_rows[0]
    assert "road_surface_z_if_available" in z_rows[0]
