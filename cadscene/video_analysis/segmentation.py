from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import math

from .models import BoundaryEvidence
from .pts import DecodedFrameIndex, DecodedFrameTimestamp


@dataclass(frozen=True)
class SegmentationConfig:
    target_min_sec: float = 45.0
    target_max_sec: float = 55.0
    target_sec: float = 50.0
    hard_max_sec: float = 60.0
    min_clip_sec: float = 0.0


@dataclass(frozen=True)
class CutCandidate:
    pts_sec: float
    motion_magnitude_px: float
    clarity_score: float


@dataclass(frozen=True)
class PlannedClip:
    start_pts_sec: float
    end_pts_sec: float
    start_boundary: BoundaryEvidence
    end_boundary: BoundaryEvidence
    needs_review: bool
    source_start_pts: int | None = None
    source_end_pts_exclusive: int | None = None
    source_time_base: Fraction | None = None
    scene_index: int = 1
    segment_index: int = 1


def _candidate_score(candidate: CutCandidate, duration: float, target: float) -> float:
    motion_penalty = min(0.7, candidate.motion_magnitude_px / 20.0)
    distance_penalty = abs(duration - target) / 20.0
    return candidate.clarity_score - motion_penalty - distance_penalty


def plan_clip_intervals(
    *,
    frame_index: DecodedFrameIndex | None = None,
    source_start_pts_sec: float | None = None,
    source_end_pts_sec: float | None = None,
    mandatory_boundaries: list[BoundaryEvidence],
    available_source_pts: list[float] | None = None,
    cut_candidates: list[CutCandidate],
    config: SegmentationConfig | None = None,
) -> list[PlannedClip]:
    if frame_index is not None:
        return _plan_decoded_frame_intervals(
            frame_index=frame_index,
            mandatory_boundaries=mandatory_boundaries,
            cut_candidates=cut_candidates,
            config=config or SegmentationConfig(),
        )
    if source_start_pts_sec is None or source_end_pts_sec is None:
        raise ValueError("source PTS range is required")
    if available_source_pts is None:
        raise ValueError("available source PTS are required")
    compatibility_index = _decoded_index_from_second_values(
        source_start_pts_sec=source_start_pts_sec,
        source_end_pts_sec=source_end_pts_sec,
        available_source_pts=available_source_pts,
    )
    return _plan_decoded_frame_intervals(
        frame_index=compatibility_index,
        mandatory_boundaries=mandatory_boundaries,
        cut_candidates=cut_candidates,
        config=config or SegmentationConfig(),
    )


def plan_single_source_interval(
    frame_index: DecodedFrameIndex,
) -> list[PlannedClip]:
    """Keep the complete decoded source as one authoritative logical clip."""

    clip = PlannedClip(
        start_pts_sec=frame_index.source_start_pts_sec,
        end_pts_sec=frame_index.source_end_pts_exclusive_sec,
        start_boundary=BoundaryEvidence(
            frame_index.source_start_pts_sec, ("source_start",), 1.0
        ),
        end_boundary=BoundaryEvidence(
            frame_index.source_end_pts_exclusive_sec, ("source_end",), 1.0
        ),
        needs_review=False,
        source_start_pts=frame_index.source_start_pts,
        source_end_pts_exclusive=frame_index.source_end_pts_exclusive,
        source_time_base=frame_index.time_base,
        scene_index=1,
        segment_index=1,
    )
    clips = [clip]
    validate_frame_partition(frame_index, clips)
    return clips


def _decoded_index_from_second_values(
    *,
    source_start_pts_sec: float,
    source_end_pts_sec: float,
    available_source_pts: list[float],
) -> DecodedFrameIndex:
    source_start = Fraction(str(source_start_pts_sec))
    source_end = Fraction(str(source_end_pts_sec))
    if source_end <= source_start:
        raise ValueError("source PTS range must have positive duration")
    available = sorted(
        {
            source_start,
            *(
                Fraction(str(value))
                for value in available_source_pts
                if source_start_pts_sec <= value < source_end_pts_sec
            ),
        }
    )
    denominator = math.lcm(
        source_start.denominator,
        source_end.denominator,
        *(value.denominator for value in available),
    )
    time_base = Fraction(1, denominator)
    end_pts = int(source_end / time_base)
    pts_values = [int(value / time_base) for value in available]
    frames = tuple(
        DecodedFrameTimestamp(
            ordinal=ordinal,
            pts=pts,
            duration_pts=(
                pts_values[ordinal + 1]
                if ordinal + 1 < len(pts_values)
                else end_pts
            )
            - pts,
            timestamp_source="compatibility_seconds",
        )
        for ordinal, pts in enumerate(pts_values)
    )
    return DecodedFrameIndex(time_base, frames)


def _plan_decoded_frame_intervals(
    *,
    frame_index: DecodedFrameIndex,
    mandatory_boundaries: list[BoundaryEvidence],
    cut_candidates: list[CutCandidate],
    config: SegmentationConfig,
) -> list[PlannedClip]:
    frame_pts = [frame.pts for frame in frame_index.frames]
    time_base = frame_index.time_base
    mandatory = _snap_and_combine_boundaries(
        mandatory_boundaries, frame_pts=frame_pts, time_base=time_base
    )
    mandatory = _assign_short_black_transitions_to_preceding_scene(mandatory)
    source_start = (
        frame_index.source_start_pts,
        BoundaryEvidence(frame_index.source_start_pts_sec, ("source_start",), 1.0),
    )
    source_end = (
        frame_index.source_end_pts_exclusive,
        BoundaryEvidence(
            frame_index.source_end_pts_exclusive_sec, ("source_end",), 1.0
        ),
    )
    spans = [source_start, *mandatory, source_end]
    snapped_candidates = [
        (_snap_pts(item.pts_sec, frame_pts, time_base), item)
        for item in cut_candidates
    ]
    clips: list[PlannedClip] = []
    for scene_index, (span_start, span_end) in enumerate(
        zip(spans, spans[1:]), start=1
    ):
        start_pts, start_boundary = span_start
        end_pts, end_boundary = span_end
        span_duration = Fraction(end_pts - start_pts) * time_base
        hard_max = Fraction(str(config.hard_max_sec))
        piece_count = math.floor(span_duration / hard_max) + 1
        span_frame_pts = tuple(pts for pts in frame_pts if start_pts < pts < end_pts)

        @lru_cache(maxsize=None)
        def can_partition(previous_pts: int, remaining_pieces: int) -> bool:
            remaining_duration = Fraction(end_pts - previous_pts) * time_base
            if remaining_pieces == 1:
                return 0 < remaining_duration < hard_max
            if not 0 < remaining_duration < hard_max * remaining_pieces:
                return False
            for next_pts in span_frame_pts:
                if next_pts <= previous_pts:
                    continue
                if Fraction(next_pts - previous_pts) * time_base >= hard_max:
                    break
                if can_partition(next_pts, remaining_pieces - 1):
                    return True
            return False

        if not can_partition(start_pts, piece_count):
            raise ValueError(
                "no authoritative decoded-frame PTS can satisfy the strict duration limit"
            )
        cut_points: list[tuple[int, BoundaryEvidence]] = []
        previous = start_pts
        for cut_index in range(1, piece_count):
            remaining_pieces = piece_count - cut_index
            ideal = Fraction(start_pts) + Fraction(end_pts - start_pts) * Fraction(
                cut_index, piece_count
            )
            feasible = [
                pts
                for pts in frame_pts
                if previous < pts < end_pts
                and Fraction(pts - previous) * time_base < hard_max
                and Fraction(end_pts - pts) * time_base
                < hard_max * remaining_pieces
                and can_partition(pts, remaining_pieces)
            ]
            if not feasible:
                raise ValueError(
                    "no authoritative decoded-frame PTS can satisfy the strict duration limit"
                )
            preferred = [
                (pts, candidate)
                for pts, candidate in snapped_candidates
                if pts in feasible
            ]
            if preferred:
                chosen_pts, candidate = max(
                    preferred,
                    key=lambda item: _candidate_score(
                        item[1],
                        float(Fraction(item[0] - start_pts) * time_base),
                        float(Fraction(ideal - start_pts) * time_base),
                    ),
                )
                confidence = 0.72
            else:
                chosen_pts = min(feasible, key=lambda pts: abs(Fraction(pts) - ideal))
                confidence = 0.62
            cut_points.append(
                (
                    chosen_pts,
                    BoundaryEvidence(
                        float(chosen_pts * time_base),
                        ("duration_preferred_cut",),
                        confidence,
                    ),
                )
            )
            previous = chosen_pts

        scene_boundaries = [(start_pts, start_boundary), *cut_points, (end_pts, end_boundary)]
        for segment_index, ((clip_start, clip_start_boundary), (clip_end, clip_end_boundary)) in enumerate(
            zip(scene_boundaries, scene_boundaries[1:]), start=1
        ):
            duration = Fraction(clip_end - clip_start) * time_base
            if duration <= 0 or duration >= hard_max:
                raise ValueError("planned clip exceeds hard duration limit")
            clips.append(
                PlannedClip(
                    start_pts_sec=float(clip_start * time_base),
                    end_pts_sec=float(clip_end * time_base),
                    start_boundary=clip_start_boundary,
                    end_boundary=clip_end_boundary,
                    needs_review=duration < Fraction(str(config.min_clip_sec)),
                    source_start_pts=clip_start,
                    source_end_pts_exclusive=clip_end,
                    source_time_base=time_base,
                    scene_index=scene_index,
                    segment_index=segment_index,
                )
            )
    validate_frame_partition(frame_index, clips)
    return clips


def _snap_and_combine_boundaries(
    boundaries: list[BoundaryEvidence],
    *,
    frame_pts: list[int],
    time_base: Fraction,
) -> list[tuple[int, BoundaryEvidence]]:
    grouped: dict[int, list[BoundaryEvidence]] = {}
    for boundary in boundaries:
        pts = _snap_pts(boundary.pts_sec, frame_pts, time_base)
        if frame_pts[0] < pts <= frame_pts[-1]:
            grouped.setdefault(pts, []).append(boundary)
    combined: list[tuple[int, BoundaryEvidence]] = []
    for pts in sorted(grouped):
        items = grouped[pts]
        combined.append(
            (
                pts,
                BoundaryEvidence(
                    float(pts * time_base),
                    tuple(
                        dict.fromkeys(
                            reason for item in items for reason in item.reasons
                        )
                    ),
                    max(item.confidence for item in items),
                ),
            )
        )
    return combined


def _assign_short_black_transitions_to_preceding_scene(
    boundaries: list[tuple[int, BoundaryEvidence]],
) -> list[tuple[int, BoundaryEvidence]]:
    normalized: list[tuple[int, BoundaryEvidence]] = []
    index = 0
    while index < len(boundaries):
        current = boundaries[index]
        if "black_frame" not in current[1].reasons:
            normalized.append(current)
            index += 1
            continue
        run = [current]
        index += 1
        while index < len(boundaries) and "black_frame" in boundaries[index][1].reasons:
            run.append(boundaries[index])
            index += 1
        normalized.append(run[-1])
    return normalized


def _snap_pts(pts_sec: float, frame_pts: list[int], time_base: Fraction) -> int:
    target = Fraction(str(pts_sec)) / time_base
    return min(frame_pts, key=lambda pts: (abs(Fraction(pts) - target), pts))


def frames_for_interval(
    frame_index: DecodedFrameIndex, interval: PlannedClip
) -> tuple[int, ...]:
    if interval.source_start_pts is None or interval.source_end_pts_exclusive is None:
        raise ValueError("clip has no authoritative integer-PTS interval")
    return tuple(
        frame.pts
        for frame in frame_index.frames
        if interval.source_start_pts <= frame.pts < interval.source_end_pts_exclusive
    )


def validate_frame_partition(
    frame_index: DecodedFrameIndex, clips: list[PlannedClip]
) -> None:
    if not clips:
        raise ValueError("frame partition must contain at least one clip")
    if clips[0].source_start_pts != frame_index.source_start_pts:
        raise ValueError("frame partition does not start at the first decoded frame")
    if clips[-1].source_end_pts_exclusive != frame_index.source_end_pts_exclusive:
        raise ValueError("frame partition does not include the exclusive source end")
    for before, after in zip(clips, clips[1:]):
        if before.source_end_pts_exclusive != after.source_start_pts:
            raise ValueError("adjacent clip intervals have a gap or overlap")
    mapped = tuple(pts for clip in clips for pts in frames_for_interval(frame_index, clip))
    expected = tuple(frame.pts for frame in frame_index.frames)
    if mapped != expected:
        raise ValueError("clip intervals do not partition decoded frames exactly once")
