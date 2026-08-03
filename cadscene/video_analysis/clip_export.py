from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any


_CLIP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_MAX_DURATION_SEC = 60.0


@dataclass(frozen=True)
class ExportClip:
    clip_id: str
    start_pts_sec: float
    end_pts_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_pts_sec - self.start_pts_sec


def load_export_clips(manifest_path: Path) -> list[ExportClip]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("clip manifest must be a JSON object")

    items = payload.get("clips")
    if not isinstance(items, list) or not items:
        raise ValueError("clip manifest must contain a non-empty clips list")

    clips: list[ExportClip] = []
    seen_ids: set[str] = set()
    previous_end: float | None = None
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each clip must be a JSON object")

        clip_id = item.get("clip_id")
        if not isinstance(clip_id, str) or not _CLIP_ID_PATTERN.fullmatch(clip_id):
            raise ValueError("clip_id is unsafe")
        if clip_id in seen_ids:
            raise ValueError(f"clip_id is duplicated: {clip_id}")

        start = _finite_number(item.get("source_start_pts_sec"), "source_start_pts_sec")
        end = _finite_number(item.get("source_end_pts_sec"), "source_end_pts_sec")
        clip = ExportClip(clip_id=clip_id, start_pts_sec=start, end_pts_sec=end)
        if clip.duration_sec <= 0:
            raise ValueError(f"clip {clip_id} must have a positive duration")
        if clip.duration_sec >= _MAX_DURATION_SEC:
            raise ValueError(f"clip {clip_id} must be shorter than 60 seconds")
        if previous_end is not None and clip.start_pts_sec < previous_end:
            raise ValueError(f"clip {clip_id} overlaps the previous clip")

        clips.append(clip)
        seen_ids.add(clip_id)
        previous_end = clip.end_pts_sec
    return clips


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number
