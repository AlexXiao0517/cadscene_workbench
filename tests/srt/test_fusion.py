from __future__ import annotations

import numpy as np
import pytest

from cadscene.srt.fusion import estimate_sfm_to_srt_sim3, select_constraint_indices
from cadscene.srt.quality import FusionConfig


def test_known_sim3_is_recovered_with_fixed_seed_and_gps_outlier_is_rejected() -> None:
    source = np.array(
        [[0.0, 0.0, 0.0], [2.0, 0.0, 0.5], [0.0, 3.0, 0.2], [2.0, 3.0, 1.0], [1.0, 1.5, 0.4], [4.0, 1.0, 0.6]]
    )
    rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    target = 2.5 * (source @ rotation.T) + np.array([10.0, -4.0, 8.0])
    target[-1] += np.array([50.0, -50.0, 20.0])

    fit = estimate_sfm_to_srt_sim3(
        source,
        target,
        FusionConfig(min_common_frames=5, min_baseline_m=3.0, ransac_seed=7, ransac_iterations=200),
    )

    assert fit.scale == pytest.approx(2.5, rel=1e-5)
    assert fit.inlier_ratio > 0.8
    assert fit.inlier_mask[-1] is False
    assert fit.rmse_m < 1e-5


def test_constraint_selection_spatially_deduplicates_repeated_gps() -> None:
    points = np.array([[0.0, 0.0, 0.0], [0.01, 0.0, 0.0], [0.02, 0.0, 0.0], [2.0, 0.0, 0.0], [4.0, 1.0, 0.0]])

    selected = select_constraint_indices(points, FusionConfig(spatial_dedupe_m=0.5))

    assert selected.tolist() == [0, 3, 4]
