from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
import re
import shutil
import subprocess

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
    if system_ffmpeg:
        return Path(system_ffmpeg)
    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise RuntimeError(
            "FFmpeg is required for authoritative PTS analysis; install the "
            "video_analysis extra or pass an explicit executable"
        ) from exc
    executable = Path(imageio_ffmpeg.get_ffmpeg_exe())
    if not executable.is_file():
        raise FileNotFoundError(f"FFmpeg executable not found: {executable}")
    return executable


_TBN_RE = re.compile(r"(?P<rate>\d+(?:\.\d+)?)(?P<suffix>[kKmM]?) tbn")
_DURATION_RE = re.compile(
    r"Duration:\s*(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?),"
    r"\s*start:\s*(?P<start>-?\d+(?:\.\d+)?)"
)


def _parse_time_base(ffmpeg_output: str) -> Fraction:
    matches = _TBN_RE.findall(ffmpeg_output)
    if not matches:
        raise ValueError("video stream time base (tbn) missing from FFmpeg output")
    rate_text, suffix = matches[0]
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[suffix.lower()]
    rate = Fraction(rate_text) * multiplier
    return Fraction(1, 1) / rate


def _parse_container_end(ffmpeg_output: str) -> float | None:
    match = _DURATION_RE.search(ffmpeg_output)
    if match is None:
        return None
    duration = (
        int(match.group("hours")) * 3600
        + int(match.group("minutes")) * 60
        + float(match.group("seconds"))
    )
    return float(match.group("start")) + duration


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
    time_base = _parse_time_base(process.stderr)
    packets = parse_debug_packet_timestamps(
        process.stderr, time_base=time_base, require_monotonic=False
    )
    packets.sort(key=lambda packet: packet.pts)
    if not packets:
        raise ValueError("video stream contains no timestamped packets")
    packet_end = packets[-1].pts_sec + packets[-1].duration_sec
    container_end = _parse_container_end(process.stderr)
    return VideoPtsIndex(
        time_base=time_base,
        packets=tuple(packets),
        source_start_pts_sec=packets[0].pts_sec,
        source_end_pts_sec=max(packet_end, container_end or packet_end),
    )


_SHOWINFO_PTS_RE = re.compile(r"\bn:\s*\d+\s+pts:\s*(?P<pts>-?\d+)")


def decode_sparse_frames(
    video_path: Path,
    *,
    index: VideoPtsIndex,
    interval_sec: float,
    output_size: tuple[int, int] = (320, 180),
    ffmpeg_executable: str | Path | None = None,
) -> list[DecodedFrame]:
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
    process = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
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
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stderr = process.stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg sparse decode failed: {stderr[-1000:]}")
    selected_pts = [int(match.group("pts")) for match in _SHOWINFO_PTS_RE.finditer(stderr)]
    frame_size = width * height
    if len(process.stdout) != len(selected_pts) * frame_size:
        raise ValueError("sparse decode frame bytes and PTS metadata disagree")
    exact_pts = {packet.pts: packet.pts_sec for packet in index.packets}
    frames: list[DecodedFrame] = []
    for frame_index, pts in enumerate(selected_pts):
        if pts not in exact_pts:
            raise ValueError(f"decoded frame PTS missing from source packet index: {pts}")
        start = frame_index * frame_size
        image = np.frombuffer(process.stdout[start : start + frame_size], dtype=np.uint8).reshape(
            height, width
        )
        frames.append(DecodedFrame(pts=pts, pts_sec=exact_pts[pts], image=image.copy()))
    return frames


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
