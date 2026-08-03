from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from cadscene.video_analysis.pts import resolve_ffmpeg_executable


_CLIP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_MAX_DURATION_SEC = 60.0
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
X264_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
)


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
        if (
            not isinstance(clip_id, str)
            or not _CLIP_ID_PATTERN.fullmatch(clip_id)
            or _is_windows_reserved_device_basename(clip_id)
        ):
            raise ValueError("clip_id is unsafe")
        casefolded_clip_id = clip_id.casefold()
        if casefolded_clip_id in seen_ids:
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
        seen_ids.add(casefolded_clip_id)
        previous_end = clip.end_pts_sec
    return clips


def export_video_clips(
    video_path: Path,
    manifest_path: Path,
    output_dir: Path,
    *,
    ffmpeg_executable: str | Path | None = None,
    preset: str = "fast",
    crf: int = 18,
) -> list[Path]:
    source = Path(video_path)
    manifest = Path(manifest_path)
    output = Path(output_dir)
    if not source.is_file():
        raise FileNotFoundError(f"video not found: {source}")
    if not manifest.is_file():
        raise FileNotFoundError(f"clip manifest not found: {manifest}")
    if output.exists():
        raise FileExistsError(f"clip output already exists: {output}")
    if not output.parent.is_dir():
        raise FileNotFoundError(f"clip output parent not found: {output.parent}")
    if preset not in X264_PRESETS:
        raise ValueError(f"unsupported x264 preset: {preset}")
    if isinstance(crf, bool) or not isinstance(crf, int) or not 0 <= crf <= 51:
        raise ValueError("crf must be an integer from 0 to 51")

    strategy = _publication_strategy()
    clips = load_export_clips(manifest)
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    reservation = _reserve_output_directory(output)
    temporary_dir: Path | None = None
    published = False
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
        )
        for clip in clips:
            clip_path = temporary_dir / f"{clip.clip_id}.mp4"
            process = subprocess.run(
                _build_ffmpeg_clip_command(
                    ffmpeg=ffmpeg,
                    source=source,
                    clip=clip,
                    clip_path=clip_path,
                    preset=preset,
                    crf=crf,
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            if process.returncode != 0:
                stderr = process.stderr.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"FFmpeg clip export failed for {clip.clip_id}: {stderr[-1000:]}"
                )
            if not clip_path.is_file() or clip_path.stat().st_size == 0:
                raise RuntimeError(
                    f"FFmpeg clip export produced an empty output for {clip.clip_id}"
                )

        _publish_output_directory(temporary_dir, output, strategy=strategy)
        published = True
        return [output / f"{clip.clip_id}.mp4" for clip in clips]
    finally:
        try:
            if temporary_dir is not None and not published:
                shutil.rmtree(temporary_dir)
        finally:
            reservation.rmdir()


def _reserve_output_directory(output: Path) -> Path:
    reservation = output.with_name(f".{output.name}.lock")
    try:
        reservation.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(f"clip output is already being exported: {output}") from exc
    try:
        if output.exists():
            raise FileExistsError(f"clip output already exists: {output}")
    except BaseException:
        reservation.rmdir()
        raise
    return reservation


def _build_ffmpeg_clip_command(
    *,
    ffmpeg: str | Path,
    source: Path,
    clip: ExportClip,
    clip_path: Path,
    preset: str,
    crf: int,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-nostdin",
        "-seek_timestamp",
        "1",
        "-ss",
        str(clip.start_pts_sec),
        "-i",
        str(source),
        "-t",
        str(clip.duration_sec),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        str(clip_path),
    ]


def _publication_strategy(platform: str | None = None) -> str:
    selected_platform = sys.platform if platform is None else platform
    if selected_platform == "win32":
        return "windows"
    if selected_platform.startswith("linux"):
        if _linux_renameat2() is None:
            raise OSError("atomic no-clobber publication requires libc renameat2")
        return "linux"
    raise OSError(
        "atomic no-clobber directory publication is unsupported on this platform"
    )


def _publish_output_directory(
    temporary_dir: Path, output: Path, *, strategy: str
) -> None:
    try:
        if strategy == "windows":
            os.rename(temporary_dir, output)
        elif strategy == "linux":
            _linux_rename_noreplace(temporary_dir, output)
        else:
            raise ValueError(f"unsupported publication strategy: {strategy}")
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) or (
            strategy == "windows" and output.exists()
        ):
            raise FileExistsError(f"clip output already exists: {output}") from exc
        raise


def _linux_renameat2():
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return None
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    return renameat2


def _is_windows_reserved_device_basename(clip_id: str) -> bool:
    basename, _, _ = clip_id.partition(".")
    return basename.casefold() in _WINDOWS_RESERVED_DEVICE_BASENAMES


def _linux_rename_noreplace(temporary_dir: Path, output: Path) -> None:
    renameat2 = _linux_renameat2()
    if renameat2 is None:
        raise OSError("atomic no-clobber publication requires libc renameat2")
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(temporary_dir),
        _AT_FDCWD,
        os.fsencode(output),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(output))


def _finite_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number
