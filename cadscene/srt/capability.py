"""Decide how safely SRT metadata can assist trajectory reconstruction."""

from collections.abc import Mapping, Sequence
from typing import Any

from .schema import SrtRecord


_POSITION_FIELDS = ("latitude", "longitude")
_GIMBAL_FIELDS = ("gimbal_yaw", "gimbal_pitch", "gimbal_roll")
_DRONE_FIELDS = ("drone_yaw", "drone_pitch", "drone_roll")
_MIN_COVERAGE = 0.8
_MIN_TRAJECTORY_RECORDS = 2


def _value(record: SrtRecord | Mapping[str, Any], field: str) -> Any:
    return getattr(record, field, None) if isinstance(record, SrtRecord) else record.get(field)


def detect_trajectory_capability(
    records: Sequence[SrtRecord | Mapping[str, Any]], video_duration_sec: float | None = None
) -> dict[str, Any]:
    """Return conservative field coverage and one of the supported modes.

    ``srt_full_pose`` is deliberately limited to complete gimbal (camera)
    attitude plus a sufficiently covered GPS/altitude trajectory. Aircraft-only
    attitude can improve SfM fusion but cannot determine camera pose.
    """

    total = len(records)
    tracked = _POSITION_FIELDS + ("altitude",) + _GIMBAL_FIELDS + _DRONE_FIELDS
    coverage = {
        field: (sum(_value(record, field) is not None for record in records) / total if total else 0.0)
        for field in tracked
    }
    fields = {field: coverage[field] > 0.0 for field in tracked}
    # The public yaw/pitch/roll fields intentionally mean camera attitude,
    # never aircraft attitude.
    fields.update(
        {
            "yaw": fields["gimbal_yaw"],
            "pitch": fields["gimbal_pitch"],
            "roll": fields["gimbal_roll"],
            "gps": all(fields[field] for field in _POSITION_FIELDS),
        }
    )
    warnings: list[str] = []

    gps_coverage = min(coverage[field] for field in _POSITION_FIELDS)
    altitude_coverage = coverage["altitude"]
    trajectory_ready = total >= _MIN_TRAJECTORY_RECORDS and gps_coverage >= _MIN_COVERAGE and altitude_coverage >= _MIN_COVERAGE
    if total == 0:
        warnings.append("SRT parse produced no usable metadata records.")
    if gps_coverage < _MIN_COVERAGE:
        warnings.append("GPS coverage is insufficient for SRT trajectory fusion.")
    if altitude_coverage < _MIN_COVERAGE:
        warnings.append("Altitude coverage is insufficient for SRT trajectory fusion.")

    gimbal_complete = all(coverage[field] >= _MIN_COVERAGE for field in _GIMBAL_FIELDS)
    drone_present = any(fields[field] for field in _DRONE_FIELDS)
    gimbal_present = any(fields[field] for field in _GIMBAL_FIELDS)
    if drone_present and not gimbal_complete:
        warnings.append("Drone attitude is not camera/gimbal attitude; full pose is unavailable.")

    if trajectory_ready and gimbal_complete:
        mode = "srt_full_pose"
    elif trajectory_ready:
        mode = "srt_sfm_fused"
    else:
        mode = "sfm_only"

    if video_duration_sec is not None and total:
        observed_end = max(float(_value(record, "end_sec") or 0.0) for record in records)
        tolerance = max(1.0, video_duration_sec * 0.1)
        if abs(observed_end - video_duration_sec) > tolerance:
            warnings.append(
                "SRT duration differs from the supplied video duration "
                f"({observed_end:.3f}s vs {video_duration_sec:.3f}s)."
            )

    return {
        "detected_mode": mode,
        "fields": fields,
        "coverage": coverage,
        "attitude_sources": {"gimbal": gimbal_present, "drone": drone_present},
        "warnings": warnings,
    }
