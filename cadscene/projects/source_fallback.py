from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Mapping

from cadscene.video_analysis.clip_export import (
    ExportClip,
    X264_PRESETS,
    build_clip_frame_map,
)
from cadscene.video_analysis.pts import (
    DecodedFrameIndex,
    DecodedFrameTimestamp,
    probe_decoded_frame_index,
    resolve_ffmpeg_executable,
)

from .adapters import AdapterProgress, AdapterResult
from .identifiers import is_safe_stable_id, validate_project_id
from .media import (
    InvalidMediaContract,
    ProjectMediaSpec,
    media_compatibility,
    probe_media,
    validate_rendered_media,
)
from .render_adapters import RenderExecutionPlan


OUTPUT_VIDEO_NAME = "rendered.mp4"
OUTPUT_FRAME_MAP_NAME = "render_frame_map.json"
_ATOMIC_WRITE_LOCKS_GUARD = threading.Lock()
_ATOMIC_WRITE_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True)
class SourceIntervalRenderInputs:
    """Immutable inputs for an explicitly confirmed original-video fallback."""

    project_id: str
    clip_id: str
    source_video_path: Path
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    attempt_directory: Path
    project_media_spec: ProjectMediaSpec

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid clip_id")
        source = self.source_video_path
        if (
            not isinstance(source, Path)
            or not source.is_absolute()
            or source.is_symlink()
            or not source.is_file()
        ):
            raise ValueError("source video must be an existing absolute regular file")
        attempt = self.attempt_directory
        if (
            not isinstance(attempt, Path)
            or not attempt.is_absolute()
            or attempt.is_symlink()
            or not attempt.is_dir()
        ):
            raise ValueError("attempt directory must be an existing absolute directory")
        if (
            type(self.source_start_pts) is not int
            or type(self.source_end_pts_exclusive) is not int
            or self.source_end_pts_exclusive <= self.source_start_pts
        ):
            raise ValueError("source interval must be increasing integer PTS")
        if (
            not isinstance(self.source_time_base, Fraction)
            or self.source_time_base <= 0
        ):
            raise ValueError("source_time_base must be a positive Fraction")
        if not isinstance(self.project_media_spec, ProjectMediaSpec):
            raise TypeError("project_media_spec must be a ProjectMediaSpec")


def select_source_interval_frames(
    frame_index: DecodedFrameIndex,
    *,
    source_start_pts: int,
    source_end_pts_exclusive: int,
    source_time_base: Fraction,
) -> tuple[DecodedFrameTimestamp, ...]:
    if frame_index.time_base != source_time_base:
        raise ValueError("source time base disagrees with decoded-frame index")
    if (
        type(source_start_pts) is not int
        or type(source_end_pts_exclusive) is not int
        or source_end_pts_exclusive <= source_start_pts
    ):
        raise ValueError("source interval must be increasing integer PTS")
    stage8a_map = build_clip_frame_map(
        frame_index,
        [
            ExportClip(
                clip_id="source-interval",
                source_start_pts=source_start_pts,
                source_end_pts_exclusive=source_end_pts_exclusive,
                source_time_base=source_time_base,
            )
        ],
        require_full_source_partition=False,
    )
    entries = stage8a_map["clips"][0]["frames"]
    return tuple(frame_index.frames[int(item["ordinal"])] for item in entries)


def build_source_render_frame_map(
    frame_index: DecodedFrameIndex,
    *,
    clip_id: str,
    source_start_pts: int,
    source_end_pts_exclusive: int,
    source_time_base: Fraction,
) -> dict[str, object]:
    if not is_safe_stable_id(clip_id):
        raise ValueError("invalid clip_id")
    selected = select_source_interval_frames(
        frame_index,
        source_start_pts=source_start_pts,
        source_end_pts_exclusive=source_end_pts_exclusive,
        source_time_base=source_time_base,
    )
    return {
        "schema_version": 1,
        "interval_semantics": "half_open",
        "clip_id": clip_id,
        "source_time_base": {
            "numerator": source_time_base.numerator,
            "denominator": source_time_base.denominator,
        },
        "source_start_pts": source_start_pts,
        "source_end_pts_exclusive": source_end_pts_exclusive,
        "frames": [
            {
                "output_frame_ordinal": output_ordinal,
                "source_decoded_frame_ordinal": frame.ordinal,
                "source_pts": frame.pts,
            }
            for output_ordinal, frame in enumerate(selected)
        ],
    }


def build_source_interval_ffmpeg_command(
    inputs: SourceIntervalRenderInputs,
    *,
    ffmpeg_executable: str | Path,
    preset: str = "fast",
    crf: int = 18,
) -> tuple[str, ...]:
    return _build_source_interval_ffmpeg_command(
        inputs,
        ffmpeg_executable=ffmpeg_executable,
        output_path=inputs.attempt_directory / OUTPUT_VIDEO_NAME,
        preset=preset,
        crf=crf,
    )


def _build_source_interval_ffmpeg_command(
    inputs: SourceIntervalRenderInputs,
    *,
    ffmpeg_executable: str | Path,
    output_path: Path,
    preset: str,
    crf: int,
) -> tuple[str, ...]:
    spec = inputs.project_media_spec
    if spec.codec_name != "h264":
        raise ValueError("source fallback currently supports the h264 project codec")
    if spec.time_base.numerator != 1:
        raise ValueError(
            "source fallback requires a project media time base numerator of one"
        )
    _validate_encoder_options(preset=preset, crf=crf)
    if not output_path.is_absolute():
        raise ValueError("output path must be absolute")
    sar = spec.sample_aspect_ratio
    filters = (
        f"trim=start_pts={inputs.source_start_pts}:"
        f"end_pts={inputs.source_end_pts_exclusive},"
        "setpts=PTS-STARTPTS,"
        f"scale={spec.width}:{spec.height}:flags=lanczos,"
        f"setsar={sar.numerator}/{sar.denominator},"
        f"format={spec.pixel_format},"
        f"setparams=range={spec.color_range}:"
        f"color_primaries={spec.color_primaries}:"
        f"color_trc={spec.color_transfer}:colorspace={spec.color_space}"
    )
    return (
        str(ffmpeg_executable),
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-nostdin",
        "-copyts",
        "-autorotate",
        "-i",
        str(inputs.source_video_path),
        "-map",
        "0:v:0",
        "-vf",
        filters,
        "-fps_mode",
        "passthrough",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-bf",
        "0",
        "-profile:v",
        spec.profile.casefold(),
        "-pix_fmt",
        spec.pixel_format,
        "-enc_time_base",
        f"{spec.time_base.numerator}/{spec.time_base.denominator}",
        "-video_track_timescale",
        str(spec.time_base.denominator),
        "-color_range",
        spec.color_range,
        "-colorspace",
        spec.color_space,
        "-color_trc",
        spec.color_transfer,
        "-color_primaries",
        spec.color_primaries,
        "-metadata:s:v:0",
        "rotate=0",
        "-movflags",
        "+faststart",
        "-use_editlist",
        "0",
        str(output_path),
    )


def render_source_interval(
    inputs: SourceIntervalRenderInputs,
    *,
    ffmpeg_executable: str | Path | None = None,
    ffprobe_executable: str | Path | None = None,
    preset: str = "fast",
    crf: int = 18,
) -> tuple[Path, Path]:
    final_video = inputs.attempt_directory / OUTPUT_VIDEO_NAME
    frame_map_path = inputs.attempt_directory / OUTPUT_FRAME_MAP_NAME
    temporary_video = inputs.attempt_directory / ".rendered.unvalidated.mp4"
    if final_video.exists() or frame_map_path.exists() or temporary_video.exists():
        raise FileExistsError("source fallback attempt outputs already exist")
    ffmpeg = resolve_ffmpeg_executable(ffmpeg_executable)
    frame_index = probe_decoded_frame_index(
        inputs.source_video_path,
        time_base=inputs.source_time_base,
        ffprobe_executable=ffprobe_executable,
        ffmpeg_executable=ffmpeg,
    )
    frame_map = build_source_render_frame_map(
        frame_index,
        clip_id=inputs.clip_id,
        source_start_pts=inputs.source_start_pts,
        source_end_pts_exclusive=inputs.source_end_pts_exclusive,
        source_time_base=inputs.source_time_base,
    )
    _write_json_atomic(frame_map_path, frame_map)
    command = _build_source_interval_ffmpeg_command(
        inputs,
        ffmpeg_executable=ffmpeg,
        output_path=temporary_video,
        preset=preset,
        crf=crf,
    )
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"FFmpeg source interval render failed: {detail}")
    _validate_files(
        inputs,
        video_path=temporary_video,
        frame_map_path=frame_map_path,
        ffprobe_executable=ffprobe_executable,
        frame_index=frame_index,
    )
    os.replace(temporary_video, final_video)
    return final_video, frame_map_path


def validate_source_interval_outputs(
    inputs: SourceIntervalRenderInputs,
    *,
    ffprobe_executable: str | Path | None = None,
    ffmpeg_executable: str | Path | None = None,
) -> AdapterResult:
    video_path = inputs.attempt_directory / OUTPUT_VIDEO_NAME
    frame_map_path = inputs.attempt_directory / OUTPUT_FRAME_MAP_NAME
    try:
        frame_index = probe_decoded_frame_index(
            inputs.source_video_path,
            time_base=inputs.source_time_base,
            ffprobe_executable=ffprobe_executable,
            ffmpeg_executable=ffmpeg_executable,
        )
        proof = _validate_files(
            inputs,
            video_path=video_path,
            frame_map_path=frame_map_path,
            ffprobe_executable=ffprobe_executable,
            frame_index=frame_index,
        )
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return AdapterResult.failed(f"source interval validation failed: {exc}")
    proof_payload = {
        "rendered_frame_count": proof["rendered_frame_count"],
        "output_pts": proof["output_pts"],
        "source_ordinals": proof["source_ordinals"],
        "source_pts": proof["source_pts"],
        "source_time_base": proof["source_time_base"],
        "project_media_spec": inputs.project_media_spec.to_dict(),
        "video_sha256": _sha256_file(video_path),
        "frame_map_sha256": _sha256_file(frame_map_path),
    }
    fingerprint = sha256(
        json.dumps(
            proof_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return AdapterResult.success(
        output_revision=f"source-fallback-{fingerprint[:16]}",
        output_fingerprint=fingerprint,
        outputs={"video": str(video_path), "frame_map": str(frame_map_path)},
        progress=(
            AdapterProgress(
                stage="validating", message="validated source interval render"
            ),
        ),
        validation_proof=proof_payload,
    )


class SourceIntervalRenderAdapter:
    """Manifest-free adapter used only after per-clip fallback confirmation."""

    name = "source_interval_fallback"
    version = "1"

    def __init__(
        self,
        *,
        ffmpeg_executable: str | Path | None = None,
        ffprobe_executable: str | Path | None = None,
        preset: str = "fast",
        crf: int = 18,
    ) -> None:
        self.ffmpeg_executable = ffmpeg_executable
        self.ffprobe_executable = ffprobe_executable
        _validate_encoder_options(preset=preset, crf=crf)
        self.preset = preset
        self.crf = crf

    def prepare(self, inputs: SourceIntervalRenderInputs) -> RenderExecutionPlan:
        if not isinstance(inputs, SourceIntervalRenderInputs):
            raise TypeError("source interval adapter requires SourceIntervalRenderInputs")
        command = [
            sys.executable,
            "-m",
            "cadscene.cli.render_source_interval",
            "--project-id",
            inputs.project_id,
            "--source-video",
            str(inputs.source_video_path),
            "--clip-id",
            inputs.clip_id,
            "--source-start-pts",
            str(inputs.source_start_pts),
            "--source-end-pts-exclusive",
            str(inputs.source_end_pts_exclusive),
            "--source-time-base",
            f"{inputs.source_time_base.numerator}/{inputs.source_time_base.denominator}",
            "--project-media-spec-json",
            json.dumps(
                inputs.project_media_spec.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "--attempt-dir",
            str(inputs.attempt_directory),
            "--preset",
            self.preset,
            "--crf",
            str(self.crf),
        ]
        if self.ffmpeg_executable is not None:
            command.extend(("--ffmpeg", str(self.ffmpeg_executable)))
        if self.ffprobe_executable is not None:
            command.extend(("--ffprobe", str(self.ffprobe_executable)))
        return RenderExecutionPlan(
            commands=(tuple(command),),
            validate=lambda: validate_source_interval_outputs(
                inputs,
                ffprobe_executable=self.ffprobe_executable,
                ffmpeg_executable=self.ffmpeg_executable,
            ),
        )


def source_interval_inputs_from_payload(
    *,
    project_id: str,
    clip_id: str,
    source_video_path: Path,
    source_start_pts: int,
    source_end_pts_exclusive: int,
    source_time_base: Fraction,
    attempt_directory: Path,
    project_media_spec: Mapping[str, object],
) -> SourceIntervalRenderInputs:
    return SourceIntervalRenderInputs(
        project_id=project_id,
        clip_id=clip_id,
        source_video_path=source_video_path,
        source_start_pts=source_start_pts,
        source_end_pts_exclusive=source_end_pts_exclusive,
        source_time_base=source_time_base,
        attempt_directory=attempt_directory,
        project_media_spec=ProjectMediaSpec.from_dict(project_media_spec),
    )


def _validate_files(
    inputs: SourceIntervalRenderInputs,
    *,
    video_path: Path,
    frame_map_path: Path,
    ffprobe_executable: str | Path | None,
    frame_index: DecodedFrameIndex,
) -> dict[str, object]:
    if video_path.is_symlink() or not video_path.is_file():
        raise InvalidMediaContract("source fallback rendered video is missing")
    if frame_map_path.is_symlink() or not frame_map_path.is_file():
        raise InvalidMediaContract("source fallback frame map is missing")
    selected = select_source_interval_frames(
        frame_index,
        source_start_pts=inputs.source_start_pts,
        source_end_pts_exclusive=inputs.source_end_pts_exclusive,
        source_time_base=inputs.source_time_base,
    )
    frame_map = json.loads(frame_map_path.read_text(encoding="utf-8"))
    if not isinstance(frame_map, Mapping):
        raise InvalidMediaContract("source fallback frame map must be an object")
    expected_header = {
        "interval_semantics": "half_open",
        "clip_id": inputs.clip_id,
        "source_start_pts": inputs.source_start_pts,
        "source_end_pts_exclusive": inputs.source_end_pts_exclusive,
    }
    if any(frame_map.get(key) != value for key, value in expected_header.items()):
        raise InvalidMediaContract(
            "source fallback frame map interval identity is inconsistent"
        )
    probed = probe_media(
        video_path,
        ffprobe_executable=(
            "ffprobe" if ffprobe_executable is None else str(ffprobe_executable)
        ),
    )
    proof = validate_rendered_media(
        probed,
        frame_map,
        expected_source_frames=selected,
        expected_source_time_base=inputs.source_time_base,
    )
    compatibility = media_compatibility(probed.video, inputs.project_media_spec)
    if not compatibility.compatible:
        raise InvalidMediaContract(
            "source fallback differs from project media specification: "
            + ", ".join(compatibility.differences)
        )
    return {
        "rendered_frame_count": proof.rendered_frame_count,
        "output_pts": list(proof.output_pts),
        "source_ordinals": [frame.ordinal for frame in selected],
        "source_pts": list(proof.source_pts),
        "source_time_base": {
            "numerator": inputs.source_time_base.numerator,
            "denominator": inputs.source_time_base.denominator,
        },
    }


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    lock_key = str(path.absolute()).casefold()
    with _ATOMIC_WRITE_LOCKS_GUARD:
        write_lock = _ATOMIC_WRITE_LOCKS.setdefault(lock_key, threading.RLock())
    with write_lock:
        _write_json_atomic_locked(path, payload)


def _write_json_atomic_locked(path: Path, payload: Mapping[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    created_stat = os.fstat(descriptor)
    try:
        if temporary.is_symlink():
            raise OSError("atomic JSON temporary path must not be a symlink")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not _same_regular_file(temporary, created_stat):
            raise OSError("atomic JSON temporary file identity changed")
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if _same_regular_file(temporary, created_stat):
            temporary.unlink()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_regular_file(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return False
    return not path.is_symlink() and os.path.samestat(current, expected)


def _validate_encoder_options(*, preset: str, crf: int) -> None:
    if preset not in X264_PRESETS:
        raise ValueError(f"unsupported x264 preset: {preset}")
    if type(crf) is not int or not 0 <= crf <= 51:
        raise ValueError("crf must be an integer from 0 to 51")
