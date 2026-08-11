from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from fractions import Fraction
from typing import Any


class MotionMode(str, Enum):
    GENERAL_MOTION = "general_motion"
    ROTATION_DOMINANT = "rotation_dominant"
    STATIC = "static"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BoundaryEvidence:
    pts_sec: float
    reasons: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pts_sec": self.pts_sec,
            "reasons": list(self.reasons),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class PtsMapping:
    clip_to_source_pts_offset_sec: float

    def clip_to_source_pts(self, clip_pts_sec: float) -> float:
        return clip_pts_sec + self.clip_to_source_pts_offset_sec

    def source_to_clip_pts(self, source_pts_sec: float) -> float:
        return source_pts_sec - self.clip_to_source_pts_offset_sec

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class LogicalClip:
    project_id: str
    clip_id: str
    source_start_pts_sec: float
    source_end_pts_sec: float
    analysis_start_pts_sec: float
    analysis_end_pts_sec: float
    start_boundary: BoundaryEvidence
    end_boundary: BoundaryEvidence
    detected_motion_mode: MotionMode
    confidence: float
    recommended_workflow: str | None
    needs_review: bool
    pts_mapping: PtsMapping
    render_order: int
    analysis_revision: str
    source_start_pts: int | None = None
    source_end_pts_exclusive: int | None = None
    source_time_base: Fraction | None = None
    scene_index: int = 1
    segment_index: int = 1

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "source_start_pts_sec": self.source_start_pts_sec,
            "source_end_pts_sec": self.source_end_pts_sec,
            "analysis_start_pts_sec": self.analysis_start_pts_sec,
            "analysis_end_pts_sec": self.analysis_end_pts_sec,
            "start_boundary": self.start_boundary.to_dict(),
            "end_boundary": self.end_boundary.to_dict(),
            "detected_motion_mode": self.detected_motion_mode.value,
            "confidence": self.confidence,
            "recommended_workflow": self.recommended_workflow,
            "needs_review": self.needs_review,
            "pts_mapping": self.pts_mapping.to_dict(),
            "render_order": self.render_order,
            "analysis_revision": self.analysis_revision,
            "scene_index": self.scene_index,
            "segment_index": self.segment_index,
        }
        if (
            self.source_start_pts is not None
            and self.source_end_pts_exclusive is not None
            and self.source_time_base is not None
        ):
            payload.update(
                {
                    "source_start_pts": self.source_start_pts,
                    "source_end_pts_exclusive": self.source_end_pts_exclusive,
                    "source_time_base": {
                        "numerator": self.source_time_base.numerator,
                        "denominator": self.source_time_base.denominator,
                    },
                    "source_end_pts_exclusive_sec": self.source_end_pts_sec,
                    "interval_semantics": "half_open",
                }
            )
        return payload
