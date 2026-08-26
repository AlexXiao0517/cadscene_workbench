from __future__ import annotations

import cv2
import numpy as np

from cadscene.video_analysis.models import BoundaryEvidence
from cadscene.video_analysis.pts import DecodedFrame
from cadscene.video_analysis.shot_detection import (
    FramePairEvidence,
    ShotDetectionConfig,
    analyze_frame_pair,
    coalesce_boundaries,
    detect_shot_boundaries,
    is_confirmed_dense_boundary,
    verify_candidate_boundaries,
)


def test_dense_verification_rejects_smooth_motion_and_keeps_an_abrupt_cut() -> None:
    base = _textured_frame()
    smooth = [
        _frame(10.0 + index * 0.1, np.roll(base, shift=index * 2, axis=1))
        for index in range(6)
    ]
    abrupt = [
        _frame(20.0, _textured_frame(4)),
        _frame(20.1, _textured_frame(4)),
        _frame(20.2, _textured_frame(99)),
        _frame(20.3, _textured_frame(99)),
    ]

    confirmed = verify_candidate_boundaries(
        [smooth, abrupt], expected_interval_sec=0.1
    )

    assert len(confirmed) == 1
    assert confirmed[0].pts_sec == 20.2
    assert "image_discontinuity" in confirmed[0].reasons


def test_dense_verification_rejects_geometric_jump_without_strong_image_change() -> None:
    evidence = FramePairEvidence(
        from_pts_sec=275.542,
        to_pts_sec=275.609,
        image_change_score=0.165,
        black_frame_score=0.0,
        exposure_jump_score=0.0,
        feature_match_count=20,
        feature_spatial_coverage=0.135,
        homography_inlier_ratio=0.8,
        flow_magnitude_px=40.0,
        flow_residual_px=41.44,
        clarity_score=0.8,
    )
    boundary = BoundaryEvidence(275.609, ("image_discontinuity",), 0.9)

    assert is_confirmed_dense_boundary(boundary, evidence) is False


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


def _evidence(
    *,
    image_change: float,
    matches: int,
    coverage: float,
    homography: float,
    flow_residual: float,
    exposure: float = 0.0,
) -> FramePairEvidence:
    return FramePairEvidence(
        from_pts_sec=1.0,
        to_pts_sec=1.5,
        image_change_score=image_change,
        black_frame_score=0.0,
        exposure_jump_score=exposure,
        feature_match_count=matches,
        feature_spatial_coverage=coverage,
        homography_inlier_ratio=homography,
        flow_magnitude_px=8.0,
        flow_residual_px=flow_residual,
        clarity_score=1.0,
    )


def test_jinhua_annotated_scene_changes_are_detected_from_multisignal_evidence() -> None:
    # Evidence measured from the seven manually annotated transitions in
    # jinhuaorigin.mp4 at a 0.5 second sparse decode interval.
    measured = [
        _evidence(image_change=.1154, matches=0, coverage=0, homography=0, flow_residual=38.62),
        _evidence(image_change=.1251, matches=52, coverage=.0908, homography=.8462, flow_residual=34.61),
        _evidence(image_change=.0984, matches=2, coverage=0, homography=0, flow_residual=6.60),
        _evidence(image_change=.1161, matches=0, coverage=0, homography=0, flow_residual=33.28),
        _evidence(image_change=.1263, matches=0, coverage=0, homography=0, flow_residual=44.46),
        _evidence(image_change=.1318, matches=0, coverage=0, homography=0, flow_residual=55.74),
        _evidence(image_change=.4000, matches=129, coverage=.2507, homography=.8992, flow_residual=367.15, exposure=.4000),
    ]
    frames = [_frame(1.0, np.full((8, 8), 128, np.uint8)), _frame(1.5, np.full((8, 8), 128, np.uint8))]

    for evidence in measured:
        boundaries = detect_shot_boundaries(
            frames,
            expected_interval_sec=.5,
            pair_evidence=[evidence],
        )
        assert boundaries, evidence


def test_moderate_image_change_without_geometric_collapse_is_not_a_cut() -> None:
    evidence = _evidence(
        image_change=.1603,
        matches=146,
        coverage=.28,
        homography=.68,
        flow_residual=2.52,
    )
    frames = [_frame(1.0, np.full((8, 8), 128, np.uint8)), _frame(1.5, np.full((8, 8), 128, np.uint8))]

    assert detect_shot_boundaries(
        frames, expected_interval_sec=.5, pair_evidence=[evidence]
    ) == []


def test_low_texture_change_without_flow_break_is_not_a_cut() -> None:
    evidence = _evidence(
        image_change=.10,
        matches=0,
        coverage=0,
        homography=0,
        flow_residual=1.0,
    )
    frames = [_frame(1.0, np.full((8, 8), 128, np.uint8)), _frame(1.5, np.full((8, 8), 128, np.uint8))]

    assert detect_shot_boundaries(
        frames, expected_interval_sec=.5, pair_evidence=[evidence]
    ) == []


def test_low_texture_brightness_change_with_unavailable_flow_is_not_a_cut() -> None:
    before = np.full((180, 320), 100, dtype=np.uint8)
    after = np.full((180, 320), 124, dtype=np.uint8)

    evidence = analyze_frame_pair(_frame(1.0, before), _frame(1.5, after))
    boundaries = detect_shot_boundaries(
        [_frame(1.0, before), _frame(1.5, after)], expected_interval_sec=.5
    )

    assert evidence.image_change_score >= .09
    assert evidence.flow_is_valid is False
    assert boundaries == []


def test_adjacent_transition_evidence_is_coalesced_at_first_source_pts() -> None:
    from cadscene.video_analysis.models import BoundaryEvidence

    merged = coalesce_boundaries(
        [
            BoundaryEvidence(10.0, ("black_frame",), .97),
            BoundaryEvidence(10.5, ("image_discontinuity",), .91),
            BoundaryEvidence(20.0, ("image_discontinuity",), .88),
        ],
        within_sec=1.0,
    )

    assert [item.pts_sec for item in merged] == [10.0, 20.0]
    assert merged[0].reasons == ("black_frame", "image_discontinuity")
    assert merged[0].confidence == .97


def test_chained_transition_evidence_coalesces_by_pairwise_adjacency() -> None:
    from cadscene.video_analysis.models import BoundaryEvidence

    merged = coalesce_boundaries(
        [
            BoundaryEvidence(10.0, ("image_discontinuity",), .90),
            BoundaryEvidence(10.75, ("exposure_discontinuity",), .91),
            BoundaryEvidence(11.5, ("black_frame",), .92),
        ],
        within_sec=1.0,
    )

    assert len(merged) == 1
    assert merged[0].pts_sec == 10.0


def test_chained_coalescing_has_bounded_width_for_rapid_cut_sequences() -> None:
    from cadscene.video_analysis.models import BoundaryEvidence

    merged = coalesce_boundaries(
        [
            BoundaryEvidence(10.0, ("image_discontinuity",), .90),
            BoundaryEvidence(10.75, ("image_discontinuity",), .90),
            BoundaryEvidence(11.5, ("image_discontinuity",), .90),
            BoundaryEvidence(12.25, ("image_discontinuity",), .90),
        ],
        within_sec=1.0,
    )

    assert [item.pts_sec for item in merged] == [10.0, 12.25]
