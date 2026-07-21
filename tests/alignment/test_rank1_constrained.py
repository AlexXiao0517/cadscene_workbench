from __future__ import annotations

import numpy as np
import pytest

from cadscene.alignment.rank1_constrained import (
    Rank1Config,
    analyze_rank1_axis,
    estimate_along_track_scale,
    project_along_track,
)


def test_rank1_axis_uses_time_order_to_resolve_pca_sign():
    times = np.arange(8, dtype=np.float64)
    points = np.column_stack([-2.0 * times, np.zeros(8), np.zeros(8)])

    analysis = analyze_rank1_axis(points, times, Rank1Config(min_baseline=1.0))

    assert analysis.rank == 1
    assert analysis.near_linear is True
    assert float(np.dot(analysis.primary_direction, points[-1] - points[0])) > 0.0
    assert analysis.direction_sign_source == "time_forward_endpoint_delta"


def test_project_along_track_uses_explicit_reference():
    points = np.asarray([[5.0, 2.0, 0.0], [8.0, 2.0, 0.0]])
    result = project_along_track(points, np.asarray([1.0, 0.0, 0.0]), points[0])
    assert np.allclose(result, [0.0, 3.0])


def test_robust_scale_recovers_known_metric_scale_and_residuals():
    u_sfm = np.linspace(-5.0, 7.0, 25)
    u_srt = 3.25 * u_sfm + 18.0

    fit = estimate_along_track_scale(u_sfm, u_srt, Rank1Config(ransac_seed=17))

    assert fit.scale == pytest.approx(3.25, rel=1e-8)
    assert fit.offset == pytest.approx(18.0, abs=1e-8)
    assert fit.inlier_count == len(u_sfm)
    assert fit.median_along_track_residual_m == pytest.approx(0.0, abs=1e-8)


def test_endpoint_outliers_do_not_control_scale():
    u_sfm = np.arange(30, dtype=np.float64)
    u_srt = 2.0 * u_sfm + 4.0
    u_srt[0] -= 120.0
    u_srt[-1] += 150.0

    fit = estimate_along_track_scale(
        u_sfm,
        u_srt,
        Rank1Config(ransac_seed=4, along_track_inlier_threshold_m=0.25),
    )

    assert fit.scale == pytest.approx(2.0, rel=1e-8)
    assert fit.inlier_count == 28
    assert fit.inlier_ratio == pytest.approx(28 / 30)


def test_rank_two_input_is_rejected_by_rank1_gate():
    t = np.linspace(0.0, 2.0 * np.pi, 30)
    points = np.column_stack([20.0 * np.cos(t), 10.0 * np.sin(t), np.zeros_like(t)])

    with pytest.raises(ValueError, match="rank=1"):
        analyze_rank1_axis(points, t, Rank1Config(min_baseline=1.0))
