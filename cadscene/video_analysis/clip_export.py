from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable

from cadscene.video_analysis.pts import (
    DecodedFrameIndex,
    probe_declared_video_frame_count,
    probe_decoded_frame_index,
    probe_fast_frame_index,
    resolve_ffmpeg_executable,
)


_CLIP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_WINDOWS_RESERVED_DEVICE_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_MAX_DURATION = Fraction(60, 1)
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
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction

    @property
    def start_pts_sec(self) -> float:
        return float(self.source_start_pts * self.source_time_base)

    @property
    def end_pts_sec(self) -> float:
        return float(self.source_end_pts_exclusive * self.source_time_base)

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
    validated_ids: list[str] = []
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
        seen_ids.add(casefolded_clip_id)
        validated_ids.append(clip_id)

    previous_end: int | None = None
    authoritative_time_base: Fraction | None = None
    for item, clip_id in zip(items, validated_ids):
        start = _integer_number(item.get("source_start_pts"), "source_start_pts")
        end = _integer_number(
            item.get("source_end_pts_exclusive"), "source_end_pts_exclusive"
        )
        time_base = _source_time_base(item.get("source_time_base"))
        if authoritative_time_base is None:
            authoritative_time_base = time_base
        elif time_base != authoritative_time_base:
            raise ValueError("all export clips must use one exact source time base")
        clip = ExportClip(
            clip_id=clip_id,
            source_start_pts=start,
            source_end_pts_exclusive=end,
            source_time_base=time_base,
        )
        duration = (
            Fraction(clip.source_end_pts_exclusive - clip.source_start_pts)
            * clip.source_time_base
        )
        if duration <= 0:
            raise ValueError(f"clip {clip_id} must have a positive duration")
        if duration >= _MAX_DURATION:
            raise ValueError(f"clip {clip_id} must be shorter than 60 seconds")
        if previous_end is not None and clip.source_start_pts < previous_end:
            raise ValueError(f"clip {clip_id} overlaps the previous clip")

        clips.append(clip)
        previous_end = clip.source_end_pts_exclusive
    return clips


def export_video_clips(
    video_path: Path,
    manifest_path: Path,
    output_dir: Path,
    *,
    ffmpeg_executable: str | Path | None = None,
    preset: str = "fast",
    crf: int = 18,
    require_full_source_partition: bool = True,
    progress_callback: Callable[[str, str, float], None] | None = None,
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

    def report(stage: str, message: str, fraction: float) -> None:
        if progress_callback is not None:
            progress_callback(stage, message, fraction)

    report("indexing_source", "正在读取源视频帧索引", 0.0)
    strategy = _publication_strategy()
    clips = load_export_clips(manifest)
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    frame_index = _probe_source_frame_index(source, ffmpeg)
    frame_map = build_clip_frame_map(
        frame_index,
        clips,
        require_full_source_partition=require_full_source_partition,
    )
    total_frames = sum(len(item["frames"]) for item in frame_map["clips"])
    completed_frames = 0
    report("preparing_export", "已建立源视频帧映射", 0.1)
    reservation = _reserve_output_directory(output)
    temporary_dir: Path | None = None
    published = False
    try:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
        )
        _write_json_atomic(temporary_dir / "clip_frame_map.json", frame_map)
        for clip in clips:
            clip_path = temporary_dir / f"{clip.clip_id}.mp4"
            expected_count = next(
                len(item["frames"])
                for item in frame_map["clips"]
                if item["clip_id"] == clip.clip_id
            )
            command = _build_ffmpeg_clip_command(
                ffmpeg=ffmpeg,
                source=source,
                clip=clip,
                clip_path=clip_path,
                preset=preset,
                crf=crf,
                source_start_pts_sec=frame_index.source_start_pts_sec,
            )
            if progress_callback is None:
                process = subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    check=False,
                )
            else:
                process = _run_ffmpeg_with_progress(
                    command,
                    clip_id=clip.clip_id,
                    expected_frames=expected_count,
                    completed_frames=completed_frames,
                    total_frames=total_frames,
                    callback=progress_callback,
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
            actual_count = _probe_output_frame_count(clip_path, ffmpeg)
            if actual_count != expected_count:
                raise RuntimeError(
                    f"exported frame count disagrees with clip_frame_map.json for "
                    f"{clip.clip_id}: expected {expected_count}, got "
                    f"{actual_count}"
                )
            completed_frames += expected_count
            report(
                "verifying_clip",
                f"已验证片段帧 {completed_frames}/{total_frames}",
                0.1 + 0.8 * completed_frames / total_frames,
            )

        report("publishing_export", "正在发布片段视频", 0.95)
        _publish_output_directory(temporary_dir, output, strategy=strategy)
        published = True
        report("complete", "片段视频准备完成", 1.0)
        return [output / f"{clip.clip_id}.mp4" for clip in clips]
    finally:
        try:
            if temporary_dir is not None and not published:
                shutil.rmtree(temporary_dir)
        finally:
            reservation.rmdir()


def _run_ffmpeg_with_progress(
    command: list[str],
    *,
    clip_id: str,
    expected_frames: int,
    completed_frames: int,
    total_frames: int,
    callback: Callable[[str, str, float], None],
) -> subprocess.CompletedProcess[bytes]:
    progress_command = [
        *command[:-1],
        "-progress",
        "pipe:1",
        "-nostats",
        command[-1],
    ]
    last_frame = -1
    with tempfile.TemporaryFile(mode="w+b") as stderr_file:
        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            key, separator, value = line.strip().partition("=")
            if separator != "=" or key != "frame":
                continue
            try:
                frame = max(0, min(expected_frames, int(value)))
            except ValueError:
                continue
            if frame <= last_frame:
                continue
            last_frame = frame
            processed = completed_frames + frame
            try:
                callback(
                    "encoding_clip",
                    f"正在导出片段帧 {processed}/{total_frames}",
                    0.1 + 0.8 * processed / total_frames,
                )
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        returncode = process.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()
    return subprocess.CompletedProcess(
        progress_command,
        returncode,
        stdout=b"",
        stderr=stderr,
    )


def _probe_source_frame_index(source: Path, ffmpeg: str | Path) -> DecodedFrameIndex:
    fast_index = probe_fast_frame_index(source, ffmpeg_executable=ffmpeg)
    if fast_index is not None:
        return fast_index
    return probe_decoded_frame_index(source, ffmpeg_executable=ffmpeg)


def _probe_output_frame_count(clip_path: Path, ffmpeg: str | Path) -> int:
    declared_count = probe_declared_video_frame_count(clip_path)
    if declared_count is not None:
        return declared_count
    return len(
        probe_decoded_frame_index(
            clip_path,
            ffmpeg_executable=ffmpeg,
        ).frames
    )


def _reserve_output_directory(output: Path) -> Path:
    reservation = output.with_name(f".{output.name}.lock")
    try:
        reservation.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(
            f"clip output is already being exported: {output}"
        ) from exc
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
    source_start_pts_sec: float = 0.0,
) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-nostdin",
        "-ss",
        f"{max(0.0, clip.start_pts_sec - source_start_pts_sec - 10.0):.12g}",
        "-copyts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        (
            f"trim=start_pts={clip.source_start_pts}:"
            f"end_pts={clip.source_end_pts_exclusive},setpts=PTS-STARTPTS"
        ),
        "-af",
        (
            f"atrim=start={clip.start_pts_sec:.12g}:end={clip.end_pts_sec:.12g},"
            "asetpts=PTS-STARTPTS"
        ),
        "-fps_mode",
        "passthrough",
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
        "-use_editlist",
        "0",
        str(clip_path),
    ]


def build_clip_frame_map(
    frame_index: DecodedFrameIndex,
    clips: list[ExportClip],
    *,
    require_full_source_partition: bool = True,
) -> dict[str, Any]:
    if not clips:
        raise ValueError("frame map requires at least one clip")
    mapped_frames: list[tuple[int, int]] = []
    clip_maps: list[dict[str, Any]] = []
    for clip in clips:
        if clip.source_time_base != frame_index.time_base:
            raise ValueError("clip time base disagrees with decoded-frame index")
        frames = [
            frame
            for frame in frame_index.frames
            if clip.source_start_pts <= frame.pts < clip.source_end_pts_exclusive
        ]
        if not frames:
            raise ValueError(f"clip {clip.clip_id} contains no decoded frames")
        entries = [{"ordinal": frame.ordinal, "pts": frame.pts} for frame in frames]
        mapped_frames.extend((frame.ordinal, frame.pts) for frame in frames)
        clip_maps.append(
            {
                "clip_id": clip.clip_id,
                "source_start_pts": clip.source_start_pts,
                "source_end_pts_exclusive": clip.source_end_pts_exclusive,
                "frames": entries,
            }
        )
    expected = [
        (frame.ordinal, frame.pts)
        for frame in frame_index.frames
        if require_full_source_partition
        or any(
            clip.source_start_pts <= frame.pts < clip.source_end_pts_exclusive
            for clip in clips
        )
    ]
    if mapped_frames != expected:
        raise ValueError("export clips do not partition decoded frames exactly once")
    if (
        require_full_source_partition
        and clips[0].source_start_pts != frame_index.source_start_pts
    ):
        raise ValueError("export clips omit the first decoded frame")
    if (
        require_full_source_partition
        and clips[-1].source_end_pts_exclusive != frame_index.source_end_pts_exclusive
    ):
        raise ValueError("export clips omit the exclusive source end")
    return {
        "schema_version": 1,
        "interval_semantics": "half_open",
        "source_time_base": {
            "numerator": frame_index.time_base.numerator,
            "denominator": frame_index.time_base.denominator,
        },
        "source_start_pts": frame_index.source_start_pts,
        "source_end_pts_exclusive": frame_index.source_end_pts_exclusive,
        "full_source_partition": require_full_source_partition,
        "clips": clip_maps,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


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


def _integer_number(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _source_time_base(value: Any) -> Fraction:
    if not isinstance(value, dict):
        raise ValueError("source_time_base must be an object")
    numerator = _integer_number(value.get("numerator"), "source_time_base.numerator")
    denominator = _integer_number(
        value.get("denominator"), "source_time_base.denominator"
    )
    if numerator <= 0 or denominator <= 0:
        raise ValueError("source_time_base must be positive")
    return Fraction(numerator, denominator)
