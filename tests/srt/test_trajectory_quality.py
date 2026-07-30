from __future__ import annotations

import numpy as np

from cadscene.srt.quality import FusionConfig, assess_fusion_readiness


def test_rank_two_non_collinear_path_is_allowed_with_low_vertical_observability() -> None:
    arc = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [4.0, 3.0, 0.0], [0.0, 3.0, 0.0]])

    report = assess_fusion_readiness(arc, arc, FusionConfig(min_common_frames=4, min_baseline_m=3.0))

    assert report.accepted is True
    assert report.vertical_observability == "low"


def test_linear_or_short_path_is_rejected() -> None:
    line = np.array([[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [6.0, 0.0, 0.0], [9.0, 0.0, 0.0]])

    report = assess_fusion_readiness(line, line, FusionConfig(min_common_frames=4, min_baseline_m=3.0))

    assert report.accepted is False
    assert "near-linear" in report.rejection_reasons
