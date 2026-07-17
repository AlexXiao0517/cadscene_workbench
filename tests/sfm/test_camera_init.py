from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cadscene.alignment.aligner import _camera_to_world_rotation
from cadscene.core.camera import CameraState
from cadscene.sfm.camera_init import load_sfm_camera_initialization


def _matrix_to_quat_wxyz(matrix: np.ndarray) -> list[float]:
    m = np.asarray(matrix, dtype=np.float64)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        return [0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    raise AssertionError("test quaternion helper only supports positive trace")


def test_sfm_camera_initialization_uses_rotation_and_fov_without_position(tmp_path: Path) -> None:
    python_state = CameraState(yaw_deg=35.0, pitch_deg=12.0, roll_deg=4.0)
    cam_from_world = _camera_to_world_rotation(python_state).T
    trajectory = tmp_path / "camera_trajectory.json"
    trajectory.write_text(
        json.dumps(
            {
                "width": 1920,
                "height": 1080,
                "intrinsics": [{"width": 1920, "height": 1080, "params": [960.0, 960.0, 960.0, 540.0]}],
                "poses": [
                    {"frame_index": 0, "registered": False},
                    {
                        "frame_index": 5,
                        "registered": True,
                        "center": [100, 200, 300],
                        "cam_from_world_quat_wxyz": _matrix_to_quat_wxyz(cam_from_world),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_sfm_camera_initialization(trajectory)

    assert result["frame_index"] == 5
    assert result["yaw"] == pytest.approx(35.0)
    assert result["pitch"] == pytest.approx(-12.0)
    assert result["roll"] == pytest.approx(4.0)
    assert result["fov"] == pytest.approx(90.0)
    assert result["orientation_safe_to_apply"] is False
    assert result["safe_fields"] == ["fov"]
    assert not {"x", "y", "z", "center"}.intersection(result)
