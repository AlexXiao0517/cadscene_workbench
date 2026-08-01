from __future__ import annotations

import cv2
import numpy as np

from cadscene.video_analysis.models import MotionMode
from cadscene.video_analysis.motion import (
    MotionAnalysisConfig,
    MotionWindow,
    classify_motion_window,
    stabilize_motion_windows,
)
from cadscene.video_analysis.shot_detection import FramePairEvidence
from cadscene.video_analysis.pts import DecodedFrame
from cadscene.video_analysis.shot_detection import analyze_frame_pair


def _evidence(
    end: float,
    *,
    flow: float,
    residual: float,
    inliers: float = 0.85,
    matches: int = 80,
    coverage: float = 0.5,
    clarity: float = 0.9,
) -> FramePairEvidence:
    return FramePairEvidence(
        from_pts_sec=end - 0.5,
        to_pts_sec=end,
        image_change_score=0.1,
        black_frame_score=0.0,
        exposure_jump_score=0.0,
        feature_match_count=matches,
        feature_spatial_coverage=coverage,
        homography_inlier_ratio=inliers,
        flow_magnitude_px=flow,
        flow_residual_px=residual,
        clarity_score=clarity,
    )


def test_window_classifier_has_four_required_motion_modes() -> None:
    rotation = classify_motion_window(
        [_evidence(0.5, flow=12.0, residual=0.6), _evidence(1.0, flow=10.0, residual=0.7)]
    )
    general = classify_motion_window(
        [
            _evidence(0.5, flow=10.0, residual=4.5, inliers=0.45),
            _evidence(1.0, flow=12.0, residual=5.0, inliers=0.4),
        ]
    )
    static = classify_motion_window(
        [_evidence(0.5, flow=0.2, residual=0.1), _evidence(1.0, flow=0.25, residual=0.1)]
    )
    unknown = classify_motion_window(
        [
            _evidence(
                0.5,
                flow=4.0,
                residual=1.0,
                matches=2,
                coverage=0.01,
                clarity=0.03,
            )
        ]
    )

    assert rotation.motion_mode is MotionMode.ROTATION_DOMINANT
    assert rotation.confidence >= 0.75
    assert general.motion_mode is MotionMode.GENERAL_MOTION
    assert general.confidence >= 0.7
    assert static.motion_mode is MotionMode.STATIC
    assert static.confidence >= 0.8
    assert unknown.motion_mode is MotionMode.UNKNOWN
    assert unknown.confidence < 0.6


def _windows(modes: list[MotionMode], confidence: float = 0.85) -> list[MotionWindow]:
    return [
        MotionWindow(
            start_pts_sec=float(index),
            end_pts_sec=float(index + 1),
            motion_mode=mode,
            confidence=confidence,
            evidence_count=4,
            median_flow_px=5.0,
            median_residual_px=1.0,
            median_homography_inlier_ratio=0.8,
        )
        for index, mode in enumerate(modes)
    ]


def test_sustained_general_to_rotation_switch_creates_one_boundary_at_run_start() -> None:
    raw = _windows(
        [MotionMode.GENERAL_MOTION] * 5 + [MotionMode.ROTATION_DOMINANT] * 5
    )

    stabilized, boundaries = stabilize_motion_windows(raw, MotionAnalysisConfig())

    assert [item.motion_mode for item in stabilized[:5]] == [MotionMode.GENERAL_MOTION] * 5
    assert [item.motion_mode for item in stabilized[5:]] == [MotionMode.ROTATION_DOMINANT] * 5
    assert len(boundaries) == 1
    assert boundaries[0].pts_sec == 5.0
    assert boundaries[0].reasons == ("motion_mode_change",)


def test_short_static_pause_between_same_modes_is_absorbed_without_fragment() -> None:
    raw = _windows(
        [MotionMode.GENERAL_MOTION] * 5
        + [MotionMode.STATIC]
        + [MotionMode.GENERAL_MOTION] * 4
    )

    stabilized, boundaries = stabilize_motion_windows(raw, MotionAnalysisConfig())

    assert {item.motion_mode for item in stabilized} == {MotionMode.GENERAL_MOTION}
    assert boundaries == []


def test_short_mode_flicker_does_not_pass_sustain_and_hysteresis() -> None:
    raw = _windows(
        [MotionMode.GENERAL_MOTION] * 4
        + [MotionMode.ROTATION_DOMINANT] * 2
        + [MotionMode.GENERAL_MOTION] * 4,
        confidence=0.8,
    )

    stabilized, boundaries = stabilize_motion_windows(raw, MotionAnalysisConfig())

    assert {item.motion_mode for item in stabilized} == {MotionMode.GENERAL_MOTION}
    assert boundaries == []


def test_rotation_compensated_parallax_distinguishes_general_from_global_warp() -> None:
    base = np.random.default_rng(7).integers(0, 256, size=(180, 320), dtype=np.uint8)
    rotation = cv2.warpAffine(
        base,
        cv2.getRotationMatrix2D((160, 90), 2.0, 1.0),
        (320, 180),
        borderMode=cv2.BORDER_REFLECT,
    )
    parallax = cv2.warpAffine(
        base,
        np.float32([[1, 0, 2], [0, 1, 0]]),
        (320, 180),
        borderMode=cv2.BORDER_REFLECT,
    )
    parallax[40:150, 92:202] = base[40:150, 80:190]

    rotation_evidence = analyze_frame_pair(
        DecodedFrame(0, 0.0, base), DecodedFrame(1, 0.5, rotation)
    )
    parallax_evidence = analyze_frame_pair(
        DecodedFrame(0, 0.0, base), DecodedFrame(1, 0.5, parallax)
    )

    assert classify_motion_window([rotation_evidence]).motion_mode is MotionMode.ROTATION_DOMINANT
    assert classify_motion_window([parallax_evidence]).motion_mode is MotionMode.GENERAL_MOTION
