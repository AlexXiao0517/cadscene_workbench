from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.spatial.transform import Rotation, Slerp


def apply_rotation_corrections(base: Mapping[str, Any], corrections: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_segment: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for correction in corrections:
        by_segment[int(correction["segment_id"])].append(correction)
    final = []
    for pose in base.get("poses", []):
        segment = int(pose["segment_id"])
        choices = sorted(by_segment.get(segment, []), key=lambda item: int(item["decoded_frame_index"]))
        base_rotation = Rotation.from_matrix(np.asarray(pose["rotation_cad_from_camera"], dtype=float))
        if not choices:
            delta = Rotation.identity()
        else:
            times = np.asarray([int(item["decoded_frame_index"]) for item in choices], dtype=float)
            if len(set(times)) != len(times):
                raise ValueError("conflicting corrections at the same decoded frame")
            deltas = Rotation.concatenate([
                Rotation.from_matrix(np.asarray(item["manual_rotation_cad_from_camera"], dtype=float))
                * Rotation.from_matrix(np.asarray(next(candidate for candidate in base["poses"] if int(candidate["decoded_frame_index"]) == int(item["decoded_frame_index"]) and int(candidate["segment_id"]) == segment)["rotation_cad_from_camera"], dtype=float)).inv()
                for item in choices
            ])
            current = float(pose["decoded_frame_index"])
            if current <= times[0]: delta = deltas[0]
            elif current >= times[-1]: delta = deltas[-1]
            else: delta = Slerp(times, deltas)([current])[0]
        rotation = delta * base_rotation
        final.append({**pose, "rotation_cad_from_camera": rotation.as_matrix().tolist(), "rotation_cam_from_cad": rotation.inv().as_matrix().tolist()})
    return {**dict(base), "orientation_source": "opengv_manual_anchor_plus_slerp_corrections", "local_position_correction_enabled": False, "poses": final}
