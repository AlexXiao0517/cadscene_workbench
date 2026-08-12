from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Mapping

import numpy as np


_PACKET_RE = re.compile(
    r"pkt_pts:(?P<pts>-?\d+).*?duration:(?P<duration>-?\d+)"
)


@dataclass(frozen=True)
class PacketTimestamp:
    pts: int
    pts_sec: float
    duration_sec: float


@dataclass(frozen=True)
class VideoPtsIndex:
    time_base: Fraction
    packets: tuple[PacketTimestamp, ...]
    source_start_pts_sec: float
    source_end_pts_sec: float


@dataclass(frozen=True)
class DecodedFrameTimestamp:
    ordinal: int
    pts: int
    duration_pts: int | None
    timestamp_source: str


@dataclass(frozen=True)
class DecodedFrameIndex:
    time_base: Fraction
    frames: tuple[DecodedFrameTimestamp, ...]
    source_start_pts: int = field(init=False)
    source_end_pts_exclusive: int = field(init=False)

    def __post_init__(self) -> None:
        if self.time_base <= 0:
            raise ValueError("decoded-frame time base must be positive")
        if not self.frames:
            raise ValueError("video stream contains no timestamped decoded frames")
        for ordinal, frame in enumerate(self.frames):
            if frame.ordinal != ordinal:
                raise ValueError("decoded-frame ordinals must match presentation order")
            if ordinal and frame.pts <= self.frames[ordinal - 1].pts:
                raise ValueError("decoded-frame PTS must be unique and increasing")

        last = self.frames[-1]
        if last.duration_pts is not None and last.duration_pts > 0:
            end_pts = last.pts + last.duration_pts
        elif len(self.frames) > 1:
            end_pts = last.pts + (last.pts - self.frames[-2].pts)
        else:
            end_pts = last.pts + 1
        object.__setattr__(self, "source_start_pts", self.frames[0].pts)
        object.__setattr__(self, "source_end_pts_exclusive", end_pts)

    @property
    def source_start_pts_sec(self) -> float:
        return float(self.source_start_pts * self.time_base)

    @property
    def source_end_pts_exclusive_sec(self) -> float:
        return float(self.source_end_pts_exclusive * self.time_base)


def parse_decoded_frame_records(
    payload: str | Mapping[str, Any], *, time_base: Fraction
) -> tuple[DecodedFrameTimestamp, ...]:
    document = json.loads(payload) if isinstance(payload, str) else payload
    records = document.get("frames")
    if not isinstance(records, list):
        raise ValueError("ffprobe decoded-frame response is missing a frames list")

    frames: list[DecodedFrameTimestamp] = []
    for ordinal, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError("ffprobe decoded-frame record must be an object")
        if record.get("pts") not in (None, "N/A"):
            pts = _integer_field(record["pts"], "pts")
            source = "pts"
        elif record.get("best_effort_timestamp") not in (None, "N/A"):
            pts = _integer_field(
                record["best_effort_timestamp"], "best_effort_timestamp"
            )
            source = "best_effort_timestamp"
        else:
            raise ValueError(f"decoded frame {ordinal} has no usable timestamp")
        duration_value = record.get("pkt_duration")
        duration = (
            None
            if duration_value in (None, "N/A")
            else _integer_field(duration_value, "pkt_duration")
        )
        frames.append(
            DecodedFrameTimestamp(
                ordinal=ordinal,
                pts=pts,
                duration_pts=duration if duration is not None and duration > 0 else None,
                timestamp_source=source,
            )
        )

    # Construction performs presentation-order ambiguity checks.
    DecodedFrameIndex(time_base, tuple(frames))
    return tuple(frames)


def probe_decoded_frame_index(
    video_path: Path,
    *,
    time_base: Fraction | None = None,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
) -> DecodedFrameIndex:
    """Probe frames; ``time_base`` remains a non-authoritative compatibility hint."""
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"video not found: {source}")
    try:
        ffprobe = _resolve_ffprobe_executable(ffprobe_executable)
    except RuntimeError:
        return _probe_decoded_frame_index_with_ffmpeg(
            source,
            ffmpeg_executable=ffmpeg_executable,
        )
    process = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pts,best_effort_timestamp,pkt_duration:stream=time_base",
            "-of",
            "json",
            str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        if ffprobe_executable is None:
            return _probe_decoded_frame_index_with_ffmpeg(
                source,
                ffmpeg_executable=ffmpeg_executable,
            )
        raise RuntimeError(f"FFprobe decoded-frame probe failed: {process.stderr[-1000:]}")
    document = json.loads(process.stdout)
    exact_time_base = _parse_ffprobe_time_base(document)
    frames = parse_decoded_frame_records(document, time_base=exact_time_base)
    return DecodedFrameIndex(exact_time_base, frames)


def _parse_ffprobe_time_base(document: Mapping[str, Any]) -> Fraction:
    streams = document.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise ValueError("FFprobe response must contain one selected video stream")
    stream = streams[0]
    if not isinstance(stream, Mapping):
        raise ValueError("FFprobe selected video stream must be an object")
    return _parse_exact_time_base(stream.get("time_base"), source="FFprobe")


def _parse_exact_time_base(value: Any, *, source: str) -> Fraction:
    if not isinstance(value, str) or not re.fullmatch(r"\d+/\d+", value):
        raise ValueError(f"{source} exact video time base is missing")
    numerator_text, denominator_text = value.split("/", 1)
    numerator, denominator = int(numerator_text), int(denominator_text)
    if numerator <= 0 or denominator <= 0:
        raise ValueError(f"{source} exact video time base must be positive")
    return Fraction(numerator, denominator)


def _resolve_ffprobe_executable(explicit: str | Path | None) -> Path:
    if explicit is not None:
        executable = Path(explicit)
        if not executable.is_file():
            raise FileNotFoundError(f"FFprobe executable not found: {executable}")
        return executable
    system_ffprobe = shutil.which("ffprobe")
    if system_ffprobe:
        return Path(system_ffprobe)
    raise RuntimeError(
        "FFprobe is required for authoritative decoded-frame indexing; "
        "install FFmpeg tools or pass an explicit executable"
    )


_SHOWINFO_FRAME_RE = re.compile(
    r"\bn:\s*(?P<ordinal>\d+)\s+pts:\s*(?P<pts>-?\d+).*?"
    r"\bduration:\s*(?P<duration>-?\d+)"
)
_SHOWINFO_TIME_BASE_RE = re.compile(
    r"\bconfig in time_base:\s*(?P<time_base>\d+/\d+)"
)


def _probe_decoded_frame_index_with_ffmpeg(
    source: Path,
    *,
    ffmpeg_executable: str | Path | None,
) -> DecodedFrameIndex:
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    process = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-copyts",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            "showinfo",
            "-fps_mode",
            "passthrough",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"FFmpeg decoded-frame probe failed: {process.stderr[-1000:]}"
        )
    time_base_match = _SHOWINFO_TIME_BASE_RE.search(process.stderr)
    exact_time_base = _parse_exact_time_base(
        time_base_match.group("time_base") if time_base_match is not None else None,
        source="FFmpeg showinfo",
    )
    frames = tuple(
        DecodedFrameTimestamp(
            ordinal=int(match.group("ordinal")),
            pts=int(match.group("pts")),
            duration_pts=(
                int(match.group("duration"))
                if int(match.group("duration")) > 0
                else None
            ),
            timestamp_source="pts",
        )
        for match in _SHOWINFO_FRAME_RE.finditer(process.stderr)
    )
    return DecodedFrameIndex(exact_time_base, frames)


def _integer_field(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if isinstance(value, float) and value != integer:
        raise ValueError(f"{field_name} must be an integer")
    return integer


@dataclass(frozen=True)
class DecodedFrame:
    pts: int
    pts_sec: float
    image: np.ndarray


def parse_debug_packet_timestamps(
    debug_output: str, *, time_base: Fraction, require_monotonic: bool = True
) -> list[PacketTimestamp]:
    packets: list[PacketTimestamp] = []
    for line in debug_output.splitlines():
        match = _PACKET_RE.search(line)
        if match is None or "demuxer ->" not in line or "type:video" not in line:
            continue
        pts = int(match.group("pts"))
        duration = int(match.group("duration"))
        if require_monotonic and packets and pts < packets[-1].pts:
            raise ValueError("non-monotonic source PTS")
        packets.append(
            PacketTimestamp(
                pts=pts,
                pts_sec=float(pts * time_base),
                duration_sec=float(duration * time_base),
            )
        )
    return packets


def resolve_ffmpeg_executable(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        executable = Path(explicit)
        if not executable.is_file():
            raise FileNotFoundError(f"FFmpeg executable not found: {executable}")
        return executable
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and _supports_libx264(Path(system_ffmpeg)):
        return Path(system_ffmpeg)
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        if system_ffmpeg:
            return Path(system_ffmpeg)
        raise RuntimeError(
            "FFmpeg is required for authoritative PTS analysis; install the "
            "video_analysis extra or pass an explicit executable"
        ) from exc
    executable = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not executable.is_file():
        raise FileNotFoundError(f"FFmpeg executable not found: {executable}")
    return executable


@lru_cache(maxsize=8)
def _supports_libx264(executable: Path) -> bool:
    """Avoid selecting decode-only LGPL builds for project clip export."""

    try:
        process = subprocess.run(
            [str(executable), "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.returncode == 0 and b"libx264" in process.stdout


_VIDEO_STREAM_RE = re.compile(
    r"Stream #0:(?P<index>\d+)(?:\[[^\]]+\])?(?:\([^)]*\))?: Video:.*?"
    r"(?P<rate>\d+(?:\.\d+)?)(?P<suffix>[kKmM]?) tbn"
)
_SELECTED_STREAM_RE = re.compile(
    r"Stream #0:(?P<index>\d+)(?:\[[^\]]+\])?\s*->\s*#0:0"
)


def parse_selected_video_time_base(ffmpeg_output: str) -> Fraction:
    mapping = _SELECTED_STREAM_RE.search(ffmpeg_output)
    selected_index = mapping.group("index") if mapping is not None else None
    candidates = [
        match
        for line in ffmpeg_output.splitlines()
        if (match := _VIDEO_STREAM_RE.search(line)) is not None
    ]
    selected = next(
        (match for match in candidates if match.group("index") == selected_index),
        None,
    )
    if selected is None:
        selected = next(
            (
                match
                for match in candidates
                if "attached pic" not in match.group(0).lower()
            ),
            None,
        )
    if selected is None:
        raise ValueError("selected video stream time base (tbn) missing from FFmpeg output")
    rate_text, suffix = selected.group("rate"), selected.group("suffix")
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix.lower()]
    rate = Fraction(rate_text) * multiplier
    return Fraction(1, 1) / rate


def probe_video_pts(
    video_path: Path, *, ffmpeg_executable: str | Path | None = None
) -> VideoPtsIndex:
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"video not found: {source}")
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    process = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-debug_ts",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg PTS probe failed: {process.stderr[-1000:]}")
    time_base = parse_selected_video_time_base(process.stderr)
    packets = parse_debug_packet_timestamps(
        process.stderr, time_base=time_base, require_monotonic=False
    )
    packets.sort(key=lambda packet: packet.pts)
    if not packets:
        raise ValueError("video stream contains no timestamped packets")
    packet_end = packets[-1].pts_sec + packets[-1].duration_sec
    return VideoPtsIndex(
        time_base=time_base,
        packets=tuple(packets),
        source_start_pts_sec=packets[0].pts_sec,
        source_end_pts_sec=packet_end,
    )


def probe_declared_video_frame_count(
    video_path: Path,
    *,
    ffprobe_executable: str | Path | None = None,
) -> int | None:
    """Read a container-declared frame count without decoding the video."""

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"video not found: {source}")
    try:
        ffprobe = _resolve_ffprobe_executable(ffprobe_executable)
        process = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames",
                "-of",
                "json",
                str(source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, RuntimeError):
        return None
    if process.returncode != 0:
        return None
    try:
        document = json.loads(process.stdout)
    except (TypeError, json.JSONDecodeError):
        return None
    streams = document.get("streams") if isinstance(document, Mapping) else None
    if not isinstance(streams, list) or len(streams) != 1:
        return None
    stream = streams[0]
    if not isinstance(stream, Mapping):
        return None
    value = stream.get("nb_frames")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        count = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value):
        count = int(value)
    else:
        return None
    return count if count > 0 else None


def probe_fast_frame_index(
    video_path: Path,
    *,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
) -> DecodedFrameIndex | None:
    """Build a packet index only when metadata proves one packet per frame."""

    try:
        declared_count = probe_declared_video_frame_count(
            video_path,
            ffprobe_executable=ffprobe_executable,
        )
        if declared_count is None:
            return None
        packet_index = probe_video_pts(
            video_path,
            ffmpeg_executable=ffmpeg_executable,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if len(packet_index.packets) != declared_count or packet_index.time_base <= 0:
        return None

    frames: list[DecodedFrameTimestamp] = []
    previous_pts: int | None = None
    seconds_per_tick = float(packet_index.time_base)
    for ordinal, packet in enumerate(packet_index.packets):
        if previous_pts is not None and packet.pts <= previous_pts:
            return None
        duration_ticks = packet.duration_sec / seconds_per_tick
        rounded_duration = round(duration_ticks)
        if (
            not math.isfinite(duration_ticks)
            or rounded_duration <= 0
            or not math.isclose(
                duration_ticks,
                rounded_duration,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
        ):
            return None
        frames.append(
            DecodedFrameTimestamp(
                ordinal=ordinal,
                pts=packet.pts,
                duration_pts=rounded_duration,
                timestamp_source="packet_pts",
            )
        )
        previous_pts = packet.pts
    try:
        return DecodedFrameIndex(packet_index.time_base, tuple(frames))
    except ValueError:
        return None


_SHOWINFO_PTS_RE = re.compile(r"\bn:\s*\d+\s+pts:\s*(?P<pts>-?\d+)")
_DECODED_DEBUG_RE = re.compile(
    r"decoder -> pts:(?P<pts>-?\d+).*?"
    r"\bduration:(?P<duration>-?\d+).*?"
    r"\btime_base:(?P<time_base>\d+/\d+)"
)


def parse_decoded_debug_frame_index(
    debug_output: str,
    *,
    packet_pts: frozenset[int] = frozenset(),
) -> DecodedFrameIndex:
    """Build the authoritative presentation-order index from FFmpeg decoder output."""

    decoder_lines = [
        line for line in debug_output.splitlines() if "decoder ->" in line
    ]
    if not decoder_lines:
        raise ValueError("FFmpeg debug output contains no decoded video frames")
    records: list[tuple[int, int | None, Fraction]] = []
    for line in decoder_lines:
        match = _DECODED_DEBUG_RE.search(line)
        if match is None:
            raise ValueError("decoded video frame has no usable presentation PTS")
        duration = int(match.group("duration"))
        records.append(
            (
                int(match.group("pts")),
                duration if duration > 0 else None,
                _parse_exact_time_base(
                    match.group("time_base"), source="FFmpeg decoder"
                ),
            )
        )
    time_bases = {record[2] for record in records}
    if len(time_bases) != 1:
        raise ValueError("decoded video frame time base changed during decoding")
    time_base = next(iter(time_bases))
    frames = tuple(
        DecodedFrameTimestamp(
            ordinal=ordinal,
            pts=frame_pts,
            duration_pts=duration,
            timestamp_source=(
                "pts" if frame_pts in packet_pts else "best_effort_timestamp"
            ),
        )
        for ordinal, (frame_pts, duration, _time_base) in enumerate(records)
    )
    return DecodedFrameIndex(time_base, frames)


def decode_indexed_sparse_frames(
    video_path: Path,
    *,
    interval_sec: float,
    output_size: tuple[int, int] = (320, 180),
    packet_pts: frozenset[int] = frozenset(),
    ffmpeg_executable: str | Path | None = None,
) -> tuple[DecodedFrameIndex, list[DecodedFrame]]:
    """Decode once, collecting every decoded PTS and selected analysis frames."""

    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"video not found: {source}")
    if interval_sec <= 0:
        raise ValueError("interval_sec must be positive")
    width, height = output_size
    if width <= 0 or height <= 0:
        raise ValueError("output dimensions must be positive")
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    escaped_interval = f"{interval_sec:.9f}"
    video_filter = (
        "select=isnan(prev_selected_t)+"
        f"gte(t-prev_selected_t\\,{escaped_interval}),"
        f"scale={width}:{height}:flags=area,format=gray,showinfo"
    )
    temporary_dir = Path(tempfile.mkdtemp(prefix="cadscene-video-analysis-"))
    raw_path = temporary_dir / "sparse.gray"
    try:
        process = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-debug_ts",
                "-copyts",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                video_filter,
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-y",
                str(raw_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        stderr = process.stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg indexed sparse decode failed: {stderr[-1000:]}")
        index = parse_decoded_debug_frame_index(
            stderr, packet_pts=packet_pts
        )
        selected_pts = [
            int(match.group("pts")) for match in _SHOWINFO_PTS_RE.finditer(stderr)
        ]
        exact_pts = {
            frame.pts: float(frame.pts * index.time_base)
            for frame in index.frames
        }
        frame_size = width * height
        if raw_path.stat().st_size != len(selected_pts) * frame_size:
            raise ValueError("sparse decode frame bytes and PTS metadata disagree")
        frames: list[DecodedFrame] = []
        with raw_path.open("rb") as stream:
            for frame_pts in selected_pts:
                if frame_pts not in exact_pts:
                    raise ValueError(
                        f"decoded frame PTS missing from source frame index: {frame_pts}"
                    )
                frame_bytes = stream.read(frame_size)
                if len(frame_bytes) != frame_size:
                    raise ValueError("sparse raw frame is truncated")
                frames.append(
                    DecodedFrame(
                        pts=frame_pts,
                        pts_sec=exact_pts[frame_pts],
                        image=np.frombuffer(frame_bytes, dtype=np.uint8)
                        .reshape(height, width)
                        .copy(),
                    )
                )
        return index, frames
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def decode_sparse_frames(
    video_path: Path,
    *,
    index: VideoPtsIndex | DecodedFrameIndex,
    interval_sec: float,
    output_size: tuple[int, int] = (320, 180),
    ffmpeg_executable: str | Path | None = None,
) -> list[DecodedFrame]:
    return list(
        iter_sparse_frames(
            video_path,
            index=index,
            interval_sec=interval_sec,
            output_size=output_size,
            ffmpeg_executable=ffmpeg_executable,
        )
    )


def iter_sparse_frames(
    video_path: Path,
    *,
    index: VideoPtsIndex | DecodedFrameIndex,
    interval_sec: float,
    output_size: tuple[int, int] = (320, 180),
    ffmpeg_executable: str | Path | None = None,
):
    if interval_sec <= 0:
        raise ValueError("interval_sec must be positive")
    width, height = output_size
    if width <= 0 or height <= 0:
        raise ValueError("output dimensions must be positive")
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    escaped_interval = f"{interval_sec:.9f}"
    video_filter = (
        "select=isnan(prev_selected_t)+"
        f"gte(t-prev_selected_t\\,{escaped_interval}),"
        f"scale={width}:{height}:flags=area,format=gray,showinfo"
    )
    temporary_dir = Path(tempfile.mkdtemp(prefix="cadscene-video-analysis-"))
    raw_path = temporary_dir / "sparse.gray"
    try:
        process = subprocess.run(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-copyts",
                    "-i",
                str(video_path),
                "-map",
                "0:v:0",
                "-vf",
                video_filter,
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-y",
                str(raw_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
        stderr = process.stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"FFmpeg sparse decode failed: {stderr[-1000:]}")
        selected_pts = [
            int(match.group("pts")) for match in _SHOWINFO_PTS_RE.finditer(stderr)
        ]
        frame_size = width * height
        if raw_path.stat().st_size != len(selected_pts) * frame_size:
            raise ValueError("sparse decode frame bytes and PTS metadata disagree")
        if isinstance(index, DecodedFrameIndex):
            exact_pts = {
                frame.pts: float(frame.pts * index.time_base)
                for frame in index.frames
            }
        else:
            exact_pts = {packet.pts: packet.pts_sec for packet in index.packets}
        with raw_path.open("rb") as stream:
            for pts in selected_pts:
                if pts not in exact_pts:
                    raise ValueError(
                        f"decoded frame PTS missing from source packet index: {pts}"
                    )
                frame_bytes = stream.read(frame_size)
                if len(frame_bytes) != frame_size:
                    raise ValueError("sparse raw frame is truncated")
                image = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(height, width)
                yield DecodedFrame(pts=pts, pts_sec=exact_pts[pts], image=image)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def choose_sparse_samples(
    packets: list[PacketTimestamp], *, interval_sec: float
) -> list[PacketTimestamp]:
    if interval_sec <= 0:
        raise ValueError("interval_sec must be positive")
    if not packets:
        return []
    chosen = [packets[0]]
    for packet in packets[1:-1]:
        if packet.pts_sec - chosen[-1].pts_sec >= interval_sec:
            chosen.append(packet)
    if packets[-1] is not chosen[-1]:
        chosen.append(packets[-1])
    return chosen
