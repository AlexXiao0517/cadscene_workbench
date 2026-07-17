"""Dependency-free parser for incremental DJI-style SRT telemetry blocks."""

import re
from typing import Any, BinaryIO

from .capability import detect_trajectory_capability
from .schema import SrtRecord


_TIMECODE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_BRACKET_VALUE = re.compile(r"\[\s*(?P<key>[^:\]]+)\s*:\s*(?P<value>[^\]]+)\]")
_TEXT_VALUE = re.compile(
    r"(?P<key>latitude|longitude|longtitude|altitude|height|"
    r"gimbal[_ ]?(?:yaw|pitch|roll)|drone[_ ]?(?:yaw|pitch|roll)|"
    r"aircraft[_ ]?(?:yaw|pitch|roll))\s*[:=]\s*(?P<value>[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ALIASES = {
    "latitude": "latitude", "lat": "latitude", "longitude": "longitude", "longtitude": "longitude", "lon": "longitude", "lng": "longitude",
    "altitude": "altitude", "alt": "altitude", "height": "altitude", "relativealtitude": "altitude", "relalt": "altitude", "absalt": "altitude",
    "gimbalyaw": "gimbal_yaw", "gimbalpitch": "gimbal_pitch", "gimbalroll": "gimbal_roll",
    "gbyaw": "gimbal_yaw", "gbpitch": "gimbal_pitch", "gbroll": "gimbal_roll",
    "camerayaw": "gimbal_yaw", "camerapitch": "gimbal_pitch", "cameraroll": "gimbal_roll",
    "droneyaw": "drone_yaw", "dronepitch": "drone_pitch", "droneroll": "drone_roll",
    "aircraftyaw": "drone_yaw", "aircraftpitch": "drone_pitch", "aircraftroll": "drone_roll",
}


def _seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _normalise_key(key: str) -> str | None:
    compact = re.sub(r"[^a-z]", "", key.lower())
    return _ALIASES.get(compact)


def _parse_values(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    pairs = list(_BRACKET_VALUE.finditer(text)) + list(_TEXT_VALUE.finditer(text))
    for pair in pairs:
        field = _normalise_key(pair.group("key"))
        if field is None:
            continue
        try:
            values[field] = float(pair.group("value").strip().split()[0])
        except ValueError:
            continue
    return values


def _parse_records(text: str) -> list[SrtRecord]:
    records: list[SrtRecord] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        timecode = _TIMECODE.search(block)
        if timecode is None:
            continue
        values = _parse_values(block[timecode.end():])
        if values:
            records.append(SrtRecord(_seconds(timecode.group("start")), _seconds(timecode.group("end")), **values))
    return records


def analyze_srt_stream(
    stream: BinaryIO, source_file: str, video_duration_sec: float | None = None
) -> dict[str, Any]:
    """Parse ``stream`` and return a stable, JSON-ready capability analysis."""

    raw = stream.read()
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    records = _parse_records(text)
    analysis = detect_trajectory_capability(records, video_duration_sec)
    analysis["source_file"] = source_file
    analysis["records"] = [record.to_dict() for record in records]
    return analysis
