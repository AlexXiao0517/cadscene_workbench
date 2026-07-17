"""Map original video frame times onto parsed SRT cue timestamps."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence

from .schema import SrtRecord


@dataclass(frozen=True)
class FrameTimestamp:
    source_frame_index: int
    extracted_index: int
    image_name: str
    pts_time_sec: float
    timestamp_source: str
    cfr_confirmed: bool


@dataclass(frozen=True)
class FrameSrtSample:
    frame_timestamp: FrameTimestamp
    srt_time_sec: float
    latitude: float | None
    longitude: float | None
    altitude: float | None
    gps_valid: bool
    height_valid: bool
    interpolated: bool
    source_entry_before: int | None
    source_entry_after: int | None
    rel_alt: float | None = None
    abs_alt: float | None = None


def load_frame_timestamps(path: str) -> list[FrameTimestamp]:
    """Load the persisted SfM frame-time table without renumbering source frames."""

    with open(path, encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"source_frame_index", "extracted_index", "image_name", "pts_time_sec", "timestamp_source", "cfr_confirmed"}
        if not required.issubset(reader.fieldnames or set()):
            raise ValueError("frame_timestamps.csv is missing required columns")
        rows: list[FrameTimestamp] = []
        for row in reader:
            pts = row.get("pts_time_sec", "").strip()
            if not pts:
                continue
            rows.append(
                FrameTimestamp(
                    source_frame_index=int(row["source_frame_index"]),
                    extracted_index=int(row["extracted_index"]),
                    image_name=row["image_name"],
                    pts_time_sec=float(pts),
                    timestamp_source=row["timestamp_source"],
                    cfr_confirmed=row["cfr_confirmed"].strip().lower() == "true",
                )
            )
    return rows


def resolve_frame_timestamps(
    source_frame_indices: Sequence[int],
    *,
    fps: float,
    pts_by_source_frame: Mapping[int, float] | None = None,
    cfr_confirmed: bool = False,
) -> list[FrameTimestamp]:
    """Resolve original-frame times, preferring persisted PTS over CFR math."""

    points = pts_by_source_frame or {}
    if not points and not cfr_confirmed:
        raise ValueError("cfr_confirmed=True is required when PTS timestamps are unavailable")
    if not points and (not isfinite(float(fps)) or float(fps) <= 0.0):
        raise ValueError("fps must be finite and positive for CFR fallback")

    out: list[FrameTimestamp] = []
    for extracted_index, source_frame_index in enumerate(source_frame_indices):
        frame = int(source_frame_index)
        raw = points.get(frame)
        if raw is not None:
            if not isfinite(float(raw)):
                raise ValueError(f"PTS is not finite for source frame {frame}")
            source = "pts_csv"
            base_time = float(raw)
        else:
            if not cfr_confirmed:
                raise ValueError(f"missing PTS for source frame {frame} and cfr_confirmed is false")
            source = "cfr_fps"
            base_time = frame / float(fps)
        out.append(
            FrameTimestamp(
                source_frame_index=frame,
                extracted_index=extracted_index,
                image_name=f"frame_{frame:06d}.png",
                pts_time_sec=base_time,
                timestamp_source=source,
                cfr_confirmed=bool(cfr_confirmed),
            )
        )
    return out


def _valid_gps(record: SrtRecord) -> bool:
    return (
        record.latitude is not None
        and record.longitude is not None
        and isfinite(float(record.latitude))
        and isfinite(float(record.longitude))
        and -90.0 <= float(record.latitude) <= 90.0
        and -180.0 <= float(record.longitude) <= 180.0
    )


def _valid_height(record: SrtRecord) -> bool:
    return any(
        value is not None and isfinite(float(value))
        for value in (record.rel_alt, record.abs_alt, record.altitude)
    )


def _interpolate_optional(before: SrtRecord, after: SrtRecord, field: str, alpha: float) -> float | None:
    first = getattr(before, field, None)
    second = getattr(after, field, None)
    if first is None or second is None or not isfinite(float(first)) or not isfinite(float(second)):
        return None
    return (1.0 - alpha) * float(first) + alpha * float(second)


def sample_srt_at_frames(
    records: Sequence[SrtRecord],
    *,
    frame_timestamps: Sequence[FrameTimestamp],
    max_interpolation_gap_sec: float,
    time_scale: float = 1.0,
    time_offset_sec: float = 0.0,
) -> list[FrameSrtSample]:
    """Linearly interpolate valid SRT fields only within bounded cue-time gaps."""

    if not isfinite(float(max_interpolation_gap_sec)) or float(max_interpolation_gap_sec) <= 0.0:
        raise ValueError("max_interpolation_gap_sec must be finite and positive")
    if not isfinite(float(time_scale)) or float(time_scale) <= 0.0:
        raise ValueError("time_scale must be finite and positive")
    if not isfinite(float(time_offset_sec)):
        raise ValueError("time_offset_sec must be finite")
    ordered = sorted(enumerate(records), key=lambda item: float(item[1].start_sec))
    out: list[FrameSrtSample] = []
    for frame in frame_timestamps:
        time = float(frame.pts_time_sec) * float(time_scale) + float(time_offset_sec)
        before = next(((index, row) for index, row in reversed(ordered) if float(row.start_sec) <= time), None)
        after = next(((index, row) for index, row in ordered if float(row.start_sec) >= time), None)
        if before is None or after is None:
            out.append(FrameSrtSample(frame, time, None, None, None, False, False, False, None, None))
            continue
        before_index, before_record = before
        after_index, after_record = after
        gap = float(after_record.start_sec) - float(before_record.start_sec)
        if gap > float(max_interpolation_gap_sec):
            out.append(FrameSrtSample(frame, time, None, None, None, False, False, False, before_index, after_index))
            continue
        alpha = 0.0 if gap <= 1e-12 else (time - float(before_record.start_sec)) / gap
        gps_valid = _valid_gps(before_record) and _valid_gps(after_record)
        height_valid = _valid_height(before_record) and _valid_height(after_record)
        latitude = (1.0 - alpha) * float(before_record.latitude) + alpha * float(after_record.latitude) if gps_valid else None
        longitude = (1.0 - alpha) * float(before_record.longitude) + alpha * float(after_record.longitude) if gps_valid else None
        altitude = _interpolate_optional(before_record, after_record, "altitude", alpha)
        if altitude is None:
            altitude = _interpolate_optional(before_record, after_record, "rel_alt", alpha)
        if altitude is None:
            altitude = _interpolate_optional(before_record, after_record, "abs_alt", alpha)
        out.append(
            FrameSrtSample(
                frame,
                time,
                latitude,
                longitude,
                altitude,
                gps_valid,
                height_valid,
                bool(before_index != after_index),
                before_index,
                after_index,
                _interpolate_optional(before_record, after_record, "rel_alt", alpha),
                _interpolate_optional(before_record, after_record, "abs_alt", alpha),
            )
        )
    return out
