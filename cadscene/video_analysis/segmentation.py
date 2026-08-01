from __future__ import annotations

from dataclasses import dataclass

from .models import BoundaryEvidence


@dataclass(frozen=True)
class SegmentationConfig:
    target_min_sec: float = 45.0
    target_max_sec: float = 55.0
    target_sec: float = 50.0
    hard_max_sec: float = 60.0
    min_clip_sec: float = 8.0


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


def _combine_boundaries(boundaries: list[BoundaryEvidence]) -> list[BoundaryEvidence]:
    grouped: dict[float, list[BoundaryEvidence]] = {}
    for boundary in boundaries:
        grouped.setdefault(boundary.pts_sec, []).append(boundary)
    combined: list[BoundaryEvidence] = []
    for pts_sec in sorted(grouped):
        items = grouped[pts_sec]
        reasons = tuple(dict.fromkeys(reason for item in items for reason in item.reasons))
        combined.append(BoundaryEvidence(pts_sec, reasons, max(item.confidence for item in items)))
    return combined


def _candidate_score(candidate: CutCandidate, duration: float, target: float) -> float:
    motion_penalty = min(0.7, candidate.motion_magnitude_px / 20.0)
    distance_penalty = abs(duration - target) / 20.0
    return candidate.clarity_score - motion_penalty - distance_penalty


def _choose_duration_boundary(
    cursor: float,
    span_end: float,
    available_pts: list[float],
    candidates: list[CutCandidate],
    config: SegmentationConfig,
) -> BoundaryEvidence:
    preferred = [
        item
        for item in candidates
        if config.target_min_sec <= item.pts_sec - cursor <= config.target_max_sec
        and item.pts_sec < span_end
    ]
    if preferred:
        chosen = max(
            preferred,
            key=lambda item: _candidate_score(item, item.pts_sec - cursor, config.target_sec),
        )
        score = _candidate_score(chosen, chosen.pts_sec - cursor, config.target_sec)
        return BoundaryEvidence(
            chosen.pts_sec,
            ("duration_preferred_cut",),
            max(0.55, min(0.9, 0.72 + score * 0.15)),
        )

    target_pts = [
        pts
        for pts in available_pts
        if config.target_min_sec <= pts - cursor <= config.target_max_sec and pts < span_end
    ]
    if target_pts:
        chosen_pts = min(target_pts, key=lambda pts: abs((pts - cursor) - config.target_sec))
        return BoundaryEvidence(chosen_pts, ("duration_preferred_cut",), 0.62)

    allowed = [
        pts
        for pts in available_pts
        if config.min_clip_sec <= pts - cursor <= config.hard_max_sec and pts < span_end
    ]
    if not allowed:
        raise ValueError("no authoritative source PTS is available before the 60 second limit")
    chosen_pts = max(allowed)
    return BoundaryEvidence(chosen_pts, ("duration_forced_60s",), 0.5)


def plan_clip_intervals(
    *,
    source_start_pts_sec: float,
    source_end_pts_sec: float,
    mandatory_boundaries: list[BoundaryEvidence],
    available_source_pts: list[float],
    cut_candidates: list[CutCandidate],
    config: SegmentationConfig | None = None,
) -> list[PlannedClip]:
    settings = config or SegmentationConfig()
    if source_end_pts_sec <= source_start_pts_sec:
        raise ValueError("source PTS range must have positive duration")
    available = sorted(
        set(
            [source_start_pts_sec, source_end_pts_sec]
            + [
                pts
                for pts in available_source_pts
                if source_start_pts_sec <= pts <= source_end_pts_sec
            ]
        )
    )
    mandatory = _combine_boundaries(
        [
            boundary
            for boundary in mandatory_boundaries
            if source_start_pts_sec < boundary.pts_sec < source_end_pts_sec
        ]
    )
    source_start = BoundaryEvidence(source_start_pts_sec, ("source_start",), 1.0)
    source_end = BoundaryEvidence(source_end_pts_sec, ("source_end",), 1.0)
    span_boundaries = [source_start, *mandatory, source_end]
    all_boundaries: list[BoundaryEvidence] = [source_start]

    for span_start, span_end_boundary in zip(span_boundaries, span_boundaries[1:]):
        cursor = span_start.pts_sec
        span_end = span_end_boundary.pts_sec
        while span_end - cursor > settings.hard_max_sec:
            duration_boundary = _choose_duration_boundary(
                cursor,
                span_end,
                available,
                cut_candidates,
                settings,
            )
            all_boundaries.append(duration_boundary)
            cursor = duration_boundary.pts_sec
        all_boundaries.append(span_end_boundary)

    clips: list[PlannedClip] = []
    for start_boundary, end_boundary in zip(all_boundaries, all_boundaries[1:]):
        duration = end_boundary.pts_sec - start_boundary.pts_sec
        if duration <= 0:
            continue
        if duration > settings.hard_max_sec + 1e-9:
            raise ValueError("planned clip exceeds hard duration limit")
        clips.append(
            PlannedClip(
                start_pts_sec=start_boundary.pts_sec,
                end_pts_sec=end_boundary.pts_sec,
                start_boundary=start_boundary,
                end_boundary=end_boundary,
                needs_review=duration < settings.min_clip_sec,
            )
        )
    return clips

