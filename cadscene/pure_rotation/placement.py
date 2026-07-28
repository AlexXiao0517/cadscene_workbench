from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from cadscene.pure_rotation.rotation_matrix import coerce_so3_matrix


def apply_global_placement(
    raw: Mapping[str, Any], *, segment_id: int, anchor_decoded_frame_index: int,
    camera_center_web: list[float], manual_rotation_cad_from_camera: list[list[float]], fov: float,
) -> dict[str, Any]:
    poses = [item for item in raw.get("poses", []) if int(item["segment_id"]) == segment_id]
    anchor = next((item for item in poses if int(item["decoded_frame_index"]) == anchor_decoded_frame_index), None)
    if anchor is None:
        raise ValueError("anchor must belong to the calibrated segment")
    center = [float(value) for value in camera_center_web]
    if len(center) != 3:
        raise ValueError("camera center must contain three values")
    r_manual = coerce_so3_matrix(manual_rotation_cad_from_camera, allow_legacy_reflection=True)
    r_anchor_local = coerce_so3_matrix(anchor["rotation_local_from_camera"])
    r_cad_from_local = r_manual @ r_anchor_local.T
    output = []
    for pose in poses:
        r_cad_from_camera = r_cad_from_local @ coerce_so3_matrix(pose["rotation_local_from_camera"])
        output.append({
            "decoded_frame_index": int(pose["decoded_frame_index"]), "pts_time_sec": float(pose["pts_time_sec"]), "segment_id": segment_id,
            "rotation_cad_from_camera": r_cad_from_camera.tolist(), "rotation_cam_from_cad": r_cad_from_camera.T.tolist(),
            "camera_center_web": center,
        })
    return {"schema_version": 1, "trajectory_mode": "pure_rotation_manual_calibrated", "coordinate_system": "web_cad_world", "translation_observable": False, "camera_center_mode": "manual_fixed", "position_source": "manual_global_placement", "absolute_orientation_source": "manual_global_anchor", "orientation_source": "opengv_plus_manual_anchor", "display_fov": float(fov), "poses": output}
