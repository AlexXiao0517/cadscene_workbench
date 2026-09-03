from __future__ import annotations

from fractions import Fraction

import cadscene.video_analysis.segmentation as segmentation
from cadscene.video_analysis.models import BoundaryEvidence
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp
from cadscene.video_analysis.segmentation import (
    CutCandidate,
    SegmentationConfig,
    plan_clip_intervals,
    plan_single_source_interval,
)


def _pts(end: int) -> list[float]:
    return [float(value) for value in range(end + 1)]


def _frame_index(*pts_values: int, duration_pts: int = 40) -> DecodedFrameIndex:
    return DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, duration_pts, "pts")
            for ordinal, pts in enumerate(pts_values)
        ),
    )


def test_hard_cut_first_new_scene_frame_belongs_only_to_next_clip() -> None:
    frame_index = _frame_index(8800, 8840, 8880, 9000, 9040, 9080)

    clips = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=[BoundaryEvidence(9.0, ("image_discontinuity",), 0.98)],
        cut_candidates=[],
    )

    assert clips[0].source_end_pts_exclusive == 9000
    assert clips[1].source_start_pts == 9000
    assert 9000 not in segmentation.frames_for_interval(frame_index, clips[0])
    assert 9000 in segmentation.frames_for_interval(frame_index, clips[1])
    segmentation.validate_frame_partition(frame_index, clips)


def test_short_black_transition_belongs_to_preceding_clip() -> None:
    frame_index = _frame_index(5000, 5040, 5080, 5120, 5160)

    clips = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=[
            BoundaryEvidence(5.04, ("black_frame",), 0.97),
            BoundaryEvidence(5.12, ("black_frame", "image_discontinuity"), 0.98),
        ],
        cut_candidates=[],
    )

    assert [(clip.source_start_pts, clip.source_end_pts_exclusive) for clip in clips] == [
        (5000, 5120),
        (5120, 5200),
    ]
    assert segmentation.frames_for_interval(frame_index, clips[0]) == (5000, 5040, 5080)


def test_nonzero_source_start_and_final_frame_are_partitioned_once() -> None:
    frame_index = _frame_index(5000, 5040, 5080)

    clips = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=[],
        cut_candidates=[],
    )

    assert clips[0].source_start_pts == 5000
    assert clips[-1].source_end_pts_exclusive == 5120
    assert segmentation.frames_for_interval(frame_index, clips[-1])[-1] == 5080
    segmentation.validate_frame_partition(frame_index, clips)


def test_single_source_interval_ignores_duration_and_scene_boundaries() -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1),
        (
            DecodedFrameTimestamp(0, 100, 30, "pts"),
            DecodedFrameTimestamp(1, 130, 30, "pts"),
            DecodedFrameTimestamp(2, 160, 1, "pts"),
        ),
    )

    clips = plan_single_source_interval(frame_index)

    assert len(clips) == 1
    assert clips[0].source_start_pts == 100
    assert clips[0].source_end_pts_exclusive == 161
    assert clips[0].start_boundary.reasons == ("source_start",)
    assert clips[0].end_boundary.reasons == ("source_end",)
    assert clips[0].scene_index == 1
    assert clips[0].segment_index == 1
    segmentation.validate_frame_partition(frame_index, clips)


def test_sparse_vfr_planner_rejects_locally_preferred_unreachable_cut() -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, 1, "pts")
            for ordinal, pts in enumerate((0, 52, 55, 113, 169))
        ),
    )

    clips = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=[],
        cut_candidates=[
            CutCandidate(52.0, motion_magnitude_px=0.0, clarity_score=1.0),
            CutCandidate(55.0, motion_magnitude_px=0.0, clarity_score=0.1),
        ],
    )

    assert [clip.source_end_pts_exclusive for clip in clips] == [55, 113, 170]
    assert all(
        Fraction(clip.source_end_pts_exclusive - clip.source_start_pts)
        * frame_index.time_base
        < 60
        for clip in clips
        if clip.source_start_pts is not None
        and clip.source_end_pts_exclusive is not None
    )


def test_exact_pts_duration_one_tick_below_sixty_remains_one_clip() -> None:
    denominator = 10**18
    end_pts_exclusive = 60 * denominator - 1
    frame_index = DecodedFrameIndex(
        Fraction(1, denominator),
        (
            DecodedFrameTimestamp(0, 0, None, "pts"),
            DecodedFrameTimestamp(1, end_pts_exclusive - 1, 1, "pts"),
        ),
    )

    clips = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=[],
        cut_candidates=[],
    )

    assert len(clips) == 1
    assert (
        Fraction(
            clips[0].source_end_pts_exclusive - clips[0].source_start_pts
        )
        * frame_index.time_base
        < 60
    )

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
