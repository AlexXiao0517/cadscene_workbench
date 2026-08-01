from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any

from cadscene.srt.schema import SrtRecord

from .models import MotionMode


class SrtCoverageKind(str, Enum):
    NONE = "none"
    PARTIAL = "partial"
    FULL_POSE = "full_pose"


@dataclass(frozen=True)
class ClipSrtCoverage:
    kind: SrtCoverageKind
    trajectory_coverage: float
    full_pose_coverage: float
    overlapping_record_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "trajectory_coverage": self.trajectory_coverage,
            "full_pose_coverage": self.full_pose_coverage,
            "overlapping_record_count": self.overlapping_record_count,
        }


@dataclass(frozen=True)
class WorkflowRecommendation:
    recommended_workflow: str
    needs_review: bool
    auto_selected: bool
    reasons: tuple[str, ...]


def _value(record: SrtRecord | Mapping[str, Any], field: str) -> Any:
    value = getattr(record, field, None) if isinstance(record, SrtRecord) else record.get(field)
    if field == "altitude" and value is None:
        alternatives = ("rel_alt", "abs_alt")
        for alternative in alternatives:
            candidate = (
                getattr(record, alternative, None)
                if isinstance(record, SrtRecord)
                else record.get(alternative)
            )
            if candidate is not None:
                return candidate
    return value


def _valid(record: SrtRecord | Mapping[str, Any], field: str) -> bool:
    try:
        value = float(_value(record, field))
    except (TypeError, ValueError):
        return False
    if not isfinite(value):
        return False
    if field == "latitude":
        return -90.0 <= value <= 90.0
    if field == "longitude":
        return -180.0 <= value <= 180.0
    return True


def _interval_coverage(intervals: list[tuple[float, float]], start: float, end: float) -> float:
    if not intervals or end <= start:
        return 0.0
    merged: list[list[float]] = []
    for interval_start, interval_end in sorted(intervals):
        clipped_start = max(start, interval_start)
        clipped_end = min(end, interval_end)
        if clipped_end <= clipped_start:
            continue
        if not merged or clipped_start > merged[-1][1]:
            merged.append([clipped_start, clipped_end])
        else:
            merged[-1][1] = max(merged[-1][1], clipped_end)
    return sum(item[1] - item[0] for item in merged) / (end - start)


def assess_clip_srt_coverage(
    records: Sequence[SrtRecord | Mapping[str, Any]],
    *,
    clip_source_start_pts_sec: float,
    clip_source_end_pts_sec: float,
    video_source_start_pts_sec: float,
    min_coverage: float = 0.8,
) -> ClipSrtCoverage:
    relative_start = clip_source_start_pts_sec - video_source_start_pts_sec
    relative_end = clip_source_end_pts_sec - video_source_start_pts_sec
    if relative_end <= relative_start:
        raise ValueError("clip SRT range must have positive duration")
    trajectory_intervals: list[tuple[float, float]] = []
    full_pose_intervals: list[tuple[float, float]] = []
    overlapping = 0
    trajectory_fields = ("latitude", "longitude", "altitude")
    full_pose_fields = trajectory_fields + ("gimbal_yaw", "gimbal_pitch", "gimbal_roll")
    for record in records:
        start = float(_value(record, "start_sec") or 0.0)
        end = float(_value(record, "end_sec") or start)
        if end <= relative_start or start >= relative_end:
            continue
        overlapping += 1
        if all(_valid(record, field) for field in trajectory_fields):
            trajectory_intervals.append((start, end))
        if all(_valid(record, field) for field in full_pose_fields):
            full_pose_intervals.append((start, end))
    trajectory_coverage = _interval_coverage(trajectory_intervals, relative_start, relative_end)
    full_pose_coverage = _interval_coverage(full_pose_intervals, relative_start, relative_end)
    if overlapping >= 2 and full_pose_coverage >= min_coverage:
        kind = SrtCoverageKind.FULL_POSE
    elif overlapping >= 2 and trajectory_coverage >= min_coverage:
        kind = SrtCoverageKind.PARTIAL
    else:
        kind = SrtCoverageKind.NONE
    return ClipSrtCoverage(kind, trajectory_coverage, full_pose_coverage, overlapping)


def recommend_workflow(
    motion_mode: MotionMode,
    motion_confidence: float,
    srt_coverage: ClipSrtCoverage,
    *,
    confidence_threshold: float = 0.6,
) -> WorkflowRecommendation:
    reasons: list[str] = []
    if srt_coverage.kind is SrtCoverageKind.FULL_POSE:
        workflow = "srt_full_pose"
        reasons.append("valid_full_pose_srt_coverage")
    elif srt_coverage.kind is SrtCoverageKind.PARTIAL:
        workflow = "srt_sfm_fused"
        reasons.append("valid_partial_srt_coverage")
    elif motion_mode is MotionMode.ROTATION_DOMINANT:
        workflow = "pure_rotation"
        reasons.append("rotation_dominant_without_srt")
    else:
        workflow = "sfm_only"
        reasons.append("general_or_conservative_without_srt")

    needs_review = (
        motion_mode in {MotionMode.UNKNOWN, MotionMode.STATIC}
        or motion_confidence < confidence_threshold
    )
    if motion_mode is MotionMode.UNKNOWN:
        reasons.append("unknown_motion_mode")
    if motion_confidence < confidence_threshold:
        reasons.append("low_motion_confidence")
    return WorkflowRecommendation(workflow, needs_review, False, tuple(reasons))

