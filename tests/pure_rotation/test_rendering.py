from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import numpy as np

from cadscene.cad.projection import world_from_camera_rotation
from cadscene.core.camera import CameraState
from cadscene.pure_rotation.rendering import write_camera_path_csv


def test_write_camera_path_csv_preserves_fixed_center_and_rotation(tmp_path: Path) -> None:
    rotation = np.asarray(
        [
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float64,
    )
    track = {
        "display_fov": 68.0,
        "poses": [
            {
                "decoded_frame_index": 12,
                "camera_center_web": [501900.0, 3206400.0, 120.0],
                "rotation_cad_from_camera": rotation.tolist(),
            },
            {
                "decoded_frame_index": 13,
                "camera_center_web": [501900.0, 3206400.0, 120.0],
                "rotation_cad_from_camera": rotation.tolist(),
            },
        ],
    }

    output = write_camera_path_csv(
        track,
        tmp_path / "pure_rotation_camera_path.csv",
        origin_xy=(501500.0, 3206300.0),
        cad_scale=0.5,
    )

    with output.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [int(row["frame_index"]) for row in rows] == [12, 13]
    assert {(float(row["camera_x"]), float(row["camera_y"]), float(row["camera_z"])) for row in rows} == {
        (200.0, 50.0, 60.0)
    }
    assert all(float(row["fov"]) == 68.0 for row in rows)
    reconstructed = world_from_camera_rotation(CameraState.from_row(rows[0]))
    assert np.allclose(reconstructed, rotation, atol=1e-8)


def test_render_pure_rotation_cli_is_available() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.render_pure_rotation", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--track" in completed.stdout
