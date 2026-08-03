from __future__ import annotations

from cadscene.video_analysis.models import BoundaryEvidence
from cadscene.video_analysis.segmentation import (
    CutCandidate,
    SegmentationConfig,
    plan_clip_intervals,
)


def _pts(end: int) -> list[float]:
    return [float(value) for value in range(end + 1)]


def test_duration_planner_uses_minimum_number_of_strictly_sub_sixty_clips() -> None:
    candidates = [
        CutCandidate(48.0, motion_magnitude_px=8.0, clarity_score=0.5),
        CutCandidate(52.0, motion_magnitude_px=0.3, clarity_score=0.95),
        CutCandidate(101.0, motion_magnitude_px=7.0, clarity_score=0.6),
        CutCandidate(105.0, motion_magnitude_px=0.2, clarity_score=0.9),
    ]

    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=118.0,
        mandatory_boundaries=[],
        available_source_pts=_pts(118),
        cut_candidates=candidates,
    )

    assert len(clips) == 2
    assert clips[0].end_boundary.reasons == ("duration_preferred_cut",)
    assert all(clip.end_pts_sec - clip.start_pts_sec < 60.0 for clip in clips)


def test_clear_shot_boundary_is_mandatory_even_when_it_creates_short_clip() -> None:
    shot = BoundaryEvidence(6.0, ("image_discontinuity",), 0.96)

    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=70.0,
        mandatory_boundaries=[shot],
        available_source_pts=_pts(70),
        cut_candidates=[],
    )

    assert clips[0].end_pts_sec == 6.0
    assert clips[0].end_boundary.reasons == ("image_discontinuity",)
    assert clips[0].needs_review is False
    assert clips[1].start_pts_sec == 6.0


def test_sixty_one_second_scene_is_balanced_into_two_clips_without_short_tail() -> None:
    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=61.0,
        mandatory_boundaries=[],
        available_source_pts=[0.0, 30.0, 31.0, 60.0, 61.0],
        cut_candidates=[],
    )

    assert [(clip.start_pts_sec, clip.end_pts_sec) for clip in clips] == [
        (0.0, 30.0),
        (30.0, 61.0),
    ]
    assert clips[0].end_boundary.reasons == ("duration_preferred_cut",)
    assert not any(clip.needs_review for clip in clips)


def test_old_short_video_remains_one_logical_clip() -> None:
    clips = plan_clip_intervals(
        source_start_pts_sec=10.25,
        source_end_pts_sec=17.75,
        mandatory_boundaries=[],
        available_source_pts=[10.25, 12.0, 17.75],
        cut_candidates=[],
    )

    assert len(clips) == 1
    assert clips[0].start_boundary.reasons == ("source_start",)
    assert clips[0].end_boundary.reasons == ("source_end",)


def test_long_span_never_produces_clip_over_sixty_seconds() -> None:
    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=310.0,
        mandatory_boundaries=[],
        available_source_pts=_pts(310),
        cut_candidates=[],
        config=SegmentationConfig(),
    )

    assert len(clips) == 6
    assert max(clip.end_pts_sec - clip.start_pts_sec for clip in clips) < 60.0
    assert clips[-1].end_pts_sec == 310.0


def test_exactly_sixty_seconds_requires_two_clips_because_limit_is_strict() -> None:
    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=60.0,
        mandatory_boundaries=[],
        available_source_pts=_pts(60),
        cut_candidates=[],
    )

    assert len(clips) == 2
    assert all(clip.end_pts_sec - clip.start_pts_sec < 60.0 for clip in clips)


def test_explicit_nonzero_minimum_remains_a_review_policy_override() -> None:
    clips = plan_clip_intervals(
        source_start_pts_sec=0.0,
        source_end_pts_sec=6.0,
        mandatory_boundaries=[],
        available_source_pts=[0.0, 6.0],
        cut_candidates=[],
        config=SegmentationConfig(min_clip_sec=8.0),
    )

    assert clips[0].needs_review is True
