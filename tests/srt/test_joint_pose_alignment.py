from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from cadscene.srt.joint_pose_alignment import solve_joint_alignment


def test_joint_alignment_recovers_shared_sim3_with_outlier() -> None:
    frames = np.arange(8, dtype=np.int64) * 30
    source = np.asarray([[i, i * i * 0.08, i * 0.15] for i in range(8)], dtype=float)
    world = Rotation.from_euler("zyx", [22, -4, 2], degrees=True).as_matrix()
    scale = 3.2
    translation = np.asarray([520000.0, 3197000.0, 145.0])
    targets = scale * (world @ source.T).T + translation
    targets[3] += [80.0, -70.0, 20.0]
    visual = Rotation.from_euler("zyx", np.column_stack((np.arange(8), -60 * np.ones(8), np.zeros(8))), degrees=True).as_matrix()
    dji = world[None, :, :] @ visual

    result = solve_joint_alignment(
        frames=frames,
        colmap_centers=source,
        colmap_world_from_camera=visual,
        srt_centers=targets,
        dji_world_from_camera=dji,
        frame_rate=30.0,
    )

    assert result.success
    assert abs(result.scale - scale) < 0.2
    clean = np.ones(len(frames), dtype=bool)
    clean[3] = False
    assert np.median(np.linalg.norm(result.corrected_centers[clean] - targets[clean], axis=1)) < 1.0
    assert result.final_cost < result.initial_cost
