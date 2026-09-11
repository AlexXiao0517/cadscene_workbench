from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

from cadscene.sfm.adaptive_sampling import select_adaptive_ordinals


def test_adaptive_sampling_keeps_one_second_base_and_half_second_motion() -> None:
    positions = np.asarray([[0, 0, 0], [1, 0, 0], [20, 0, 0], [21, 0, 0], [22, 0, 0]], dtype=float)
    rotations = Rotation.from_euler("z", [0, 0, 2, 2, 2], degrees=True).as_matrix()
    result = select_adaptive_ordinals(positions, rotations, np.full(5, 100.0))

    assert result.selected_ordinals == (0, 1, 2, 4)
    assert "fast_translation" in result.reasons[1]
    assert "attitude_change" in result.reasons[1]


def test_adaptive_sampling_rescues_sharper_neighbor() -> None:
    positions = np.column_stack((np.arange(5), np.zeros(5), np.zeros(5)))
    rotations = np.repeat(np.eye(3)[None, :, :], 5, axis=0)
    result = select_adaptive_ordinals(positions, rotations, np.asarray([10, 100, 100, 100, 100], dtype=float))

    assert 1 in result.selected_ordinals
    assert result.reasons[1] == ("sharpness_rescue",)
