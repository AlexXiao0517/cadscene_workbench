from __future__ import annotations

import numpy as np

from cadscene.sfm.trajectory import SfmTrajectory


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
