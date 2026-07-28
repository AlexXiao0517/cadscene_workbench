from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import numpy as np

from cadscene.core.camera import decompose_world_from_camera_rotation
from cadscene.core.io import write_csv_utf8_sig
from cadscene.pure_rotation.rotation_matrix import coerce_so3_matrix


def write_camera_path_csv(track: Mapping[str, Any], output: str | Path) -> Path:
    """Convert a fixed-center Pure-Rotation track to the overlay renderer CSV."""

    poses = list(track.get("poses") or [])
    if not poses:
        raise ValueError("pure-rotation track has no poses")
    default_fov = float(track.get("display_fov", track.get("fov", 70.0)))
    rows: list[dict[str, Any]] = []
    reference_center: np.ndarray | None = None
    for pose in poses:
        center = np.asarray(pose.get("camera_center_web"), dtype=np.float64)
        if center.shape != (3,) or not np.isfinite(center).all():
            raise ValueError("pure-rotation pose has invalid camera center")
        if reference_center is None:
            reference_center = center
        elif not np.allclose(center, reference_center, atol=1e-9):
            raise ValueError("pure-rotation camera center must remain fixed")
        rotation = coerce_so3_matrix(pose.get("rotation_cad_from_camera"))
        yaw, pitch, roll = decompose_world_from_camera_rotation(rotation)
        rows.append(
            {
                "frame_index": int(pose["decoded_frame_index"]),
                "camera_x": float(center[0]),
                "camera_y": float(center[1]),
                "camera_z": float(center[2]),
                "yaw": float(yaw),
                "pitch": float(pitch),
                "roll": float(roll),
                "fov": float(pose.get("display_fov", default_fov)),
                "status": "ok",
            }
        )
    destination = Path(output)
    write_csv_utf8_sig(destination, rows)
    return destination
