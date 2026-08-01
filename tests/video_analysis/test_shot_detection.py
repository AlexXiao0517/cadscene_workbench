from __future__ import annotations

import cv2
import numpy as np

from cadscene.video_analysis.pts import DecodedFrame
from cadscene.video_analysis.shot_detection import (
    ShotDetectionConfig,
    analyze_frame_pair,
    detect_shot_boundaries,
)


def _textured_frame(seed: int = 4) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 256, size=(180, 320), dtype=np.uint8)
    for index in range(24):
        center = (20 + (index * 47) % 280, 18 + (index * 31) % 145)
        cv2.circle(image, center, 5 + index % 7, int((index * 37) % 255), 2)
    return image


def _same_histogram_discontinuity(image: np.ndarray) -> np.ndarray:
    blocks = [block.copy() for row in np.vsplit(image, 6) for block in np.hsplit(row, 8)]
    order = np.random.default_rng(19).permutation(len(blocks))
    rows = [np.hstack([blocks[order[row * 8 + col]] for col in range(8)]) for row in range(6)]
    return np.vstack(rows)


def _frame(pts: float, image: np.ndarray) -> DecodedFrame:
    return DecodedFrame(pts=round(pts * 1000), pts_sec=pts, image=image)


def test_hard_cut_is_detected_when_color_histogram_is_identical() -> None:
    before = _textured_frame()
    after = _same_histogram_discontinuity(before)

    evidence = analyze_frame_pair(_frame(1.0, before), _frame(1.5, after))
    boundaries = detect_shot_boundaries(
        [_frame(1.0, before), _frame(1.5, after)], expected_interval_sec=0.5
    )

    assert np.array_equal(np.histogram(before, bins=32)[0], np.histogram(after, bins=32)[0])
    assert evidence.image_change_score > 0.3
    assert evidence.feature_match_count < ShotDetectionConfig().min_feature_matches or (
        evidence.feature_spatial_coverage < ShotDetectionConfig().min_feature_coverage
        or evidence.homography_inlier_ratio < ShotDetectionConfig().min_homography_inlier_ratio
        or evidence.flow_residual_px > ShotDetectionConfig().max_flow_residual_px
    )
    assert boundaries[0].pts_sec == 1.5
    assert "image_discontinuity" in boundaries[0].reasons


def test_coherent_translation_has_homography_and_flow_continuity_not_a_cut() -> None:
    before = _textured_frame()
    transform = np.float32([[1, 0, 13], [0, 1, -7]])
    after = cv2.warpAffine(before, transform, (before.shape[1], before.shape[0]))

    evidence = analyze_frame_pair(_frame(2.0, before), _frame(2.5, after))
    boundaries = detect_shot_boundaries(
        [_frame(2.0, before), _frame(2.5, after)], expected_interval_sec=0.5
    )

    assert evidence.feature_match_count >= ShotDetectionConfig().min_feature_matches
    assert evidence.homography_inlier_ratio >= ShotDetectionConfig().min_homography_inlier_ratio
    assert evidence.flow_residual_px <= ShotDetectionConfig().max_flow_residual_px
    assert boundaries == []


def test_black_frame_and_exposure_break_are_explicit_boundary_reasons() -> None:
    normal = _textured_frame()
    black = np.zeros_like(normal)

    boundaries = detect_shot_boundaries(
        [_frame(3.0, normal), _frame(3.5, black)], expected_interval_sec=0.5
    )

    assert boundaries[0].pts_sec == 3.5
    assert "black_frame" in boundaries[0].reasons
    assert "exposure_discontinuity" in boundaries[0].reasons
    assert boundaries[0].confidence >= 0.9


def test_pts_gap_creates_decode_anomaly_boundary_even_with_same_image() -> None:
    image = _textured_frame()

    boundaries = detect_shot_boundaries(
        [_frame(4.0, image), _frame(7.0, image)], expected_interval_sec=0.5
    )

    assert boundaries[0].pts_sec == 7.0
    assert boundaries[0].reasons == ("pts_or_decode_anomaly",)
    assert boundaries[0].confidence >= 0.8


def test_weak_feature_pair_alone_is_not_mislabeled_as_a_cut() -> None:
    flat = np.full((180, 320), 128, dtype=np.uint8)

    evidence = analyze_frame_pair(_frame(8.0, flat), _frame(8.5, flat))
    boundaries = detect_shot_boundaries(
        [_frame(8.0, flat), _frame(8.5, flat)], expected_interval_sec=0.5
    )

    assert evidence.feature_match_count == 0
    assert boundaries == []

