from __future__ import annotations

from cadscene.srt.schema import SrtRecord
from cadscene.video_analysis.models import MotionMode
from cadscene.video_analysis.recommendation import (
    SrtCoverageKind,
    assess_clip_srt_coverage,
    recommend_workflow,
)


def _records(*, full_pose: bool, start: int = 0, end: int = 10) -> list[SrtRecord]:
    return [
        SrtRecord(
            start_sec=float(second),
            end_sec=float(second + 1),
            latitude=30.0 + second * 1e-5,
            longitude=120.0 + second * 1e-5,
            altitude=50.0,
            gimbal_yaw=1.0 if full_pose else None,
            gimbal_pitch=-20.0 if full_pose else None,
            gimbal_roll=0.0 if full_pose else None,
        )
        for second in range(start, end)
    ]


def test_full_pose_srt_coverage_has_first_recommendation_precedence() -> None:
    coverage = assess_clip_srt_coverage(
        _records(full_pose=True),
        clip_source_start_pts_sec=100.0,
        clip_source_end_pts_sec=110.0,
        video_source_start_pts_sec=100.0,
    )

    recommendation = recommend_workflow(MotionMode.ROTATION_DOMINANT, 0.9, coverage)

    assert coverage.kind is SrtCoverageKind.FULL_POSE
    assert coverage.full_pose_coverage == 1.0
    assert recommendation.recommended_workflow == "srt_full_pose"
    assert recommendation.needs_review is False


def test_partial_srt_coverage_recommends_fusion_before_motion_workflow() -> None:
    coverage = assess_clip_srt_coverage(
        _records(full_pose=False),
        clip_source_start_pts_sec=0.0,
        clip_source_end_pts_sec=10.0,
        video_source_start_pts_sec=0.0,
    )

    recommendation = recommend_workflow(MotionMode.ROTATION_DOMINANT, 0.9, coverage)

    assert coverage.kind is SrtCoverageKind.PARTIAL
    assert recommendation.recommended_workflow == "srt_sfm_fused"


def test_srt_file_without_temporal_clip_coverage_does_not_override_motion() -> None:
    coverage = assess_clip_srt_coverage(
        _records(full_pose=True, start=20, end=30),
        clip_source_start_pts_sec=0.0,
        clip_source_end_pts_sec=10.0,
        video_source_start_pts_sec=0.0,
    )

    recommendation = recommend_workflow(MotionMode.ROTATION_DOMINANT, 0.88, coverage)

    assert coverage.kind is SrtCoverageKind.NONE
    assert recommendation.recommended_workflow == "pure_rotation"


def test_no_srt_general_motion_recommends_sfm_only() -> None:
    coverage = assess_clip_srt_coverage(
        [],
        clip_source_start_pts_sec=0.0,
        clip_source_end_pts_sec=20.0,
        video_source_start_pts_sec=0.0,
    )

    recommendation = recommend_workflow(MotionMode.GENERAL_MOTION, 0.8, coverage)

    assert recommendation.recommended_workflow == "sfm_only"
    assert recommendation.needs_review is False


def test_unknown_or_low_confidence_is_review_only_and_never_auto_selects() -> None:
    no_srt = assess_clip_srt_coverage(
        [],
        clip_source_start_pts_sec=0.0,
        clip_source_end_pts_sec=20.0,
        video_source_start_pts_sec=0.0,
    )

    unknown = recommend_workflow(MotionMode.UNKNOWN, 0.2, no_srt)
    low_rotation = recommend_workflow(MotionMode.ROTATION_DOMINANT, 0.45, no_srt)

    assert unknown.recommended_workflow == "sfm_only"
    assert unknown.needs_review is True
    assert low_rotation.recommended_workflow == "pure_rotation"
    assert low_rotation.needs_review is True
    assert unknown.auto_selected is False
    assert low_rotation.auto_selected is False
