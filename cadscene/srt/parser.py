"""Dependency-free parser for incremental DJI-style SRT telemetry blocks."""

import codecs
import re
import warnings
from typing import Any, BinaryIO

from .capability import detect_trajectory_capability
from .schema import SrtRecord


_TIMECODE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[,.]\d{3})"
)
_BRACKET_VALUE = re.compile(r"\[\s*(?P<key>[^:\]]+)\s*:\s*(?P<value>[^\]]+)\]")
_TEXT_VALUE = re.compile(
    r"(?P<key>latitude|longitude|longtitude|altitude|height|relative[_ ]?altitude|absolute[_ ]?altitude|rel[_ ]?alt|abs[_ ]?alt|"
    r"gimbal[_ ]?(?:yaw|pitch|roll)|drone[_ ]?(?:yaw|pitch|roll)|"
    r"aircraft[_ ]?(?:yaw|pitch|roll))\s*[:=]\s*(?P<value>[-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_ALIASES = {
    "latitude": "latitude", "lat": "latitude", "longitude": "longitude", "longtitude": "longitude", "lon": "longitude", "lng": "longitude",
    "altitude": "altitude", "alt": "altitude", "height": "altitude",
    "relativealtitude": "rel_alt", "relalt": "rel_alt",
    "absolutealtitude": "abs_alt", "absalt": "abs_alt",
    "gimbalyaw": "gimbal_yaw", "gimbalpitch": "gimbal_pitch", "gimbalroll": "gimbal_roll",
    "gbyaw": "gimbal_yaw", "gbpitch": "gimbal_pitch", "gbroll": "gimbal_roll",
    "camerayaw": "gimbal_yaw", "camerapitch": "gimbal_pitch", "cameraroll": "gimbal_roll",
    "droneyaw": "drone_yaw", "dronepitch": "drone_pitch", "droneroll": "drone_roll",
    "aircraftyaw": "drone_yaw", "aircraftpitch": "drone_pitch", "aircraftroll": "drone_roll",
}
_READ_CHUNK_SIZE = 64 * 1024
# These caps keep malformed uploads from retaining data proportional to stream size.
# Oversized unterminated lines and blocks are discarded with a RuntimeWarning; parsing
# resumes at the next newline or blank-line block separator, respectively.
_MAX_PENDING_CHARS = _READ_CHUNK_SIZE
_MAX_BLOCK_CHARS = 4 * _READ_CHUNK_SIZE
_MAX_BLOCK_LINES = 4096


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


def _parse_record_block(block: str) -> SrtRecord | None:
    timecode = _TIMECODE.search(block)
    if timecode is None:
        return None
    values = _parse_values(block[timecode.end():])
    if not values:
        return None
    return SrtRecord(_seconds(timecode.group("start")), _seconds(timecode.group("end")), **values)


def _parse_records(stream: BinaryIO) -> list[SrtRecord]:
    """Decode SRT incrementally, discarding malformed oversized lines or blocks."""

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    records: list[SrtRecord] = []
    block_lines: list[str] = []
    block_chars = 0
    pending = ""
    discarding_unterminated_line = False
    discarding_block = False

    def finish_block() -> None:
        nonlocal block_chars
        if not block_lines:
            return
        record = _parse_record_block("\n".join(block_lines))
        block_lines.clear()
        block_chars = 0
        if record is not None:
            records.append(record)

    def consume_line(line: str) -> None:
        nonlocal block_chars, discarding_block
        if line.strip():
            if discarding_block:
                return
            line = line.rstrip("\r")
            if len(line) > _MAX_PENDING_CHARS:
                warnings.warn("discarding oversized SRT line", RuntimeWarning, stacklevel=2)
                return
            if len(block_lines) >= _MAX_BLOCK_LINES or block_chars + len(line) > _MAX_BLOCK_CHARS:
                block_lines.clear()
                block_chars = 0
                discarding_block = True
                warnings.warn("discarding oversized SRT block", RuntimeWarning, stacklevel=2)
                return
            block_lines.append(line)
            block_chars += len(line)
        else:
            finish_block()
            discarding_block = False

    def consume_text(text: str) -> None:
        nonlocal pending, discarding_unterminated_line
        if discarding_unterminated_line:
            newline = text.find("\n")
            if newline < 0:
                return
            text = text[newline + 1 :]
            discarding_unterminated_line = False
        pending += text
        lines = pending.split("\n")
        pending = lines.pop()
        for line in lines:
            consume_line(line)
        if len(pending) > _MAX_PENDING_CHARS:
            pending = ""
            discarding_unterminated_line = True
            warnings.warn("discarding oversized unterminated SRT line", RuntimeWarning, stacklevel=2)

    while True:
        raw = stream.read(_READ_CHUNK_SIZE)
        if not raw:
            break
        consume_text(decoder.decode(raw, final=False) if isinstance(raw, bytes) else str(raw))

    consume_text(decoder.decode(b"", final=True))
    if pending and not discarding_unterminated_line:
        consume_line(pending)
    finish_block()
    return records


def analyze_srt_stream(
    stream: BinaryIO, source_file: str, video_duration_sec: float | None = None
) -> dict[str, Any]:
    """Parse ``stream`` and return a stable, JSON-ready capability analysis."""

    records = _parse_records(stream)
    analysis = detect_trajectory_capability(records, video_duration_sec)
    analysis["source_file"] = source_file
    analysis["records"] = [record.to_dict() for record in records]
    return analysis
