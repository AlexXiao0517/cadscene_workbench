from __future__ import annotations

from typing import Any, Mapping

import numpy as np


def _rotation(value: object) -> list[list[float]]:
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("invalid rotation matrix")
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6) or not np.isclose(np.linalg.det(matrix), 1.0, atol=1e-6):
        raise ValueError("invalid rotation matrix")
    return matrix.tolist()


def convert_poc_raw_trajectory(
    payload: Mapping[str, Any], *, pts_by_decoded_index: Mapping[int, float]
) -> dict[str, Any]:
    """Convert the audited POC ``world_from_camera`` convention without inversion."""
    if payload.get("trajectory_mode") not in {None, "pure_rotation_only"}:
        raise ValueError("unsupported POC trajectory mode")
    poses = []
    for source in payload.get("poses", []):
        index = int(source["decoded_frame_index"])
        center = np.asarray(source.get("camera_center"), dtype=float)
        if center.shape != (3,) or not np.allclose(center, np.zeros(3), atol=0.0):
            raise ValueError("POC camera center must be exactly fixed at zero")
        rotation = _rotation(source.get("rotation_world_from_camera"))
        xyzw = source.get("quaternion_xyzw")
        if not isinstance(xyzw, list) or len(xyzw) != 4:
            raise ValueError("POC quaternion_xyzw is required")
        poses.append(
            {
                "decoded_frame_index": index,
                "pts_time_sec": float(pts_by_decoded_index[index]),
                "segment_id": int(source["segment_id"]),
                "segment_local_index": sum(1 for item in poses if item["segment_id"] == int(source["segment_id"])),
                "segment_reference_decoded_frame_index": index if not any(item["segment_id"] == int(source["segment_id"]) for item in poses) else next(item["segment_reference_decoded_frame_index"] for item in poses if item["segment_id"] == int(source["segment_id"])),
                "rotation_local_from_camera": rotation,
                "quaternion_wxyz": [float(xyzw[3]), float(xyzw[0]), float(xyzw[1]), float(xyzw[2])],
                "camera_center_local": [0.0, 0.0, 0.0],
            }
        )
    return {
        "schema_version": 1,
        "trajectory_mode": "pure_rotation_only",
        "coordinate_system": "local_rotation_frame",
        "translation_observable": False,
        "absolute_orientation_observable": False,
        "camera_center_mode": "fixed",
        "orientation_source": "opengv_rotation_only",
        "intrinsics_source": str(payload.get("intrinsics_source", "unverified_same_resolution_candidate")),
        "intrinsics_verified": bool(payload.get("intrinsics_verified", False)),
        "poses": poses,
    }
