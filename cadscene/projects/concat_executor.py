from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from cadscene.video_analysis.pts import DecodedFrameTimestamp

from .adapters import AdapterProgress, AdapterResult
from .identifiers import is_safe_stable_id
from .concat_adapters import (
    ConcatMediaExecutionPlan,
    ConcatSegmentExecution,
    project_media_spec_fingerprint,
)
from .media import (
    InvalidMediaContract,
    ProbedMedia,
    ProjectMediaSpec,
    media_compatibility,
    probe_media,
    validate_audio_video_duration,
    validate_rendered_media,
)


CommandRunner = Callable[[tuple[str, ...]], None]
MediaProbe = Callable[[Path], ProbedMedia]


def execution_plan_from_payload(payload: Mapping[str, object]) -> ConcatMediaExecutionPlan:
    """Restore only the immutable, JSON-safe executor snapshot (never a callback)."""
    if not isinstance(payload, Mapping):
        raise TypeError("concat execution payload must be an object")
    expected_keys = {
        "project_id", "project_revision", "clips_revision",
        "project_media_spec_revision", "project_media_spec_fingerprint",
        "source_asset_fingerprint", "segments",
        "audio_source", "audio_policy", "attempt_directory",
        "video_only_output", "final_output",
        "final_frame_map_path", "final_frame_map", "expected_video_duration",
        "audio_video_tolerance",
    }
    unknown = set(payload) - expected_keys
    missing = expected_keys - set(payload)
    if unknown or missing:
        detail = sorted(unknown or missing)
        raise ValueError("concat execution payload has unknown or missing fields: " + ", ".join(detail))
    if payload.get("audio_policy") != "original_video":
        raise ValueError("concat execution audio policy must be original_video")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("concat execution segments must be a non-empty list")
    segments = tuple(_segment_from_payload(item) for item in raw_segments)
    if len({item.clip_id for item in segments}) != len(segments):
        raise ValueError("concat segment clip_id values must be unique")
    final_map = payload.get("final_frame_map")
    if not isinstance(final_map, Mapping):
        raise ValueError("concat execution final frame map must be an object")
    return ConcatMediaExecutionPlan(
        project_id=_text(payload.get("project_id"), "project_id"),
        project_revision=_integer(payload.get("project_revision"), "project_revision"),
        clips_revision=_integer(payload.get("clips_revision"), "clips_revision"),
        project_media_spec_revision=_text(payload.get("project_media_spec_revision"), "project_media_spec_revision"),
        project_media_spec_fingerprint=_sha(
            payload.get("project_media_spec_fingerprint"),
            "project_media_spec_fingerprint",
        ),
        source_asset_fingerprint=_sha(payload.get("source_asset_fingerprint"), "source_asset_fingerprint"),
        segments=segments,
        audio_source=_absolute_path(payload.get("audio_source"), "audio_source"),
        attempt_directory=_absolute_path(
            payload.get("attempt_directory"), "attempt_directory"
        ),
        video_only_output=_absolute_path(payload.get("video_only_output"), "video_only_output"),
        final_output=_absolute_path(payload.get("final_output"), "final_output"),
        final_frame_map_path=_absolute_path(payload.get("final_frame_map_path"), "final_frame_map_path"),
        final_frame_map=final_map,
        expected_video_duration=_fraction_from_payload(payload.get("expected_video_duration"), "expected_video_duration"),
        audio_video_tolerance=_fraction_from_payload(payload.get("audio_video_tolerance"), "audio_video_tolerance"),
        validate=lambda: AdapterResult.failed("deserialized concat snapshot has no publication validator"),
    )


def _segment_from_payload(value: object) -> ConcatSegmentExecution:
    if not isinstance(value, Mapping):
        raise ValueError("concat segment snapshot must be an object")
    expected = {
        "clip_id", "render_order", "source_video", "source_frame_map",
        "source_video_sha256", "source_frame_map_sha256", "concat_input",
        "needs_normalize", "input_output_revision", "input_output_fingerprint",
        "input_proof_fingerprint", "input_publication_operation_id",
    }
    if set(value) != expected:
        raise ValueError("concat segment snapshot has unknown or missing fields")
    normalize = value.get("needs_normalize")
    if type(normalize) is not bool:
        raise ValueError("needs_normalize must be boolean")
    clip_id = _text(value.get("clip_id"), "clip_id")
    if not is_safe_stable_id(clip_id):
        raise ValueError("concat segment clip_id is invalid")
    return ConcatSegmentExecution(
        clip_id=clip_id,
        render_order=_integer(value.get("render_order"), "render_order"),
        source_video=_absolute_path(value.get("source_video"), "source_video"),
        source_frame_map=_absolute_path(value.get("source_frame_map"), "source_frame_map"),
        source_video_sha256=_sha(value.get("source_video_sha256"), "source_video_sha256"),
        source_frame_map_sha256=_sha(value.get("source_frame_map_sha256"), "source_frame_map_sha256"),
        concat_input=_absolute_path(value.get("concat_input"), "concat_input"),
        needs_normalize=normalize,
        input_output_revision=_text(value.get("input_output_revision"), "input_output_revision"),
        input_output_fingerprint=_sha(value.get("input_output_fingerprint"), "input_output_fingerprint"),
        input_proof_fingerprint=_sha(value.get("input_proof_fingerprint"), "input_proof_fingerprint"),
        input_publication_operation_id=_text(value.get("input_publication_operation_id"), "input_publication_operation_id"),
    )


def build_normalize_ffmpeg_command(
    execution: ConcatMediaExecutionPlan,
    segment: ConcatSegmentExecution,
    *,
    project_media_spec: ProjectMediaSpec,
    ffmpeg_executable: str | Path,
    output_path: Path,
) -> tuple[str, ...]:
    _require_execution_and_spec(execution, project_media_spec)
    if segment not in execution.segments or not segment.needs_normalize:
        raise ValueError("normalization requires a marked concat segment")
    _require_attempt_output(output_path, execution, "normalized output")
    spec = project_media_spec
    if spec.codec_name != "h264" or spec.time_base.numerator != 1:
        raise ValueError("concat normalization supports h264 and unit-numerator time base")
    sar = spec.sample_aspect_ratio
    filters = (
        "setpts=PTS-STARTPTS,"
        f"settb=expr={spec.time_base.numerator}/{spec.time_base.denominator},"
        f"scale={spec.width}:{spec.height}:flags=lanczos,"
        f"setsar={sar.numerator}/{sar.denominator},"
        f"format={spec.pixel_format},"
        f"setparams=range={spec.color_range}:color_primaries={spec.color_primaries}:"
        f"color_trc={spec.color_transfer}:colorspace={spec.color_space}"
    )
    return (
        str(ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-n",
        "-nostdin", "-i", str(segment.source_video), "-map", "0:v:0",
        "-vf", filters, "-fps_mode", "passthrough", "-an", "-c:v", "libx264",
        "-preset", "fast", "-crf", "18", "-bf", "0", "-profile:v",
        spec.profile.casefold(), "-pix_fmt", spec.pixel_format, "-enc_time_base",
        f"{spec.time_base.numerator}/{spec.time_base.denominator}",
        "-video_track_timescale", str(spec.time_base.denominator), "-color_range",
        spec.color_range, "-colorspace", spec.color_space, "-color_trc",
        spec.color_transfer, "-color_primaries", spec.color_primaries,
        "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart",
        "-use_editlist", "0", str(output_path),
    )


def build_concat_ffmpeg_command(
    execution: ConcatMediaExecutionPlan,
    *,
    project_media_spec: ProjectMediaSpec,
    concat_list_path: Path,
    ffmpeg_executable: str | Path,
    output_path: Path,
) -> tuple[str, ...]:
    _require_execution_and_spec(execution, project_media_spec)
    _require_regular_file(concat_list_path, "concat list")
    _require_attempt_output(output_path, execution, "video-only output")
    return (
        str(ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-n",
        "-nostdin", "-f", "concat", "-safe", "0", "-i",
        str(concat_list_path), "-map", "0:v:0", "-c:v", "copy", "-an",
        "-avoid_negative_ts", "disabled", "-video_track_timescale",
        str(project_media_spec.time_base.denominator), "-movflags", "+faststart",
        "-use_editlist", "0", str(output_path),
    )


def build_audio_mux_ffmpeg_command(
    execution: ConcatMediaExecutionPlan,
    *,
    project_media_spec: ProjectMediaSpec,
    ffmpeg_executable: str | Path,
    input_video: Path,
    output_path: Path,
) -> tuple[str, ...]:
    _require_execution_and_spec(execution, project_media_spec)
    _require_regular_file(input_video, "video-only concat")
    _require_attempt_output(output_path, execution, "mux output")
    duration = _decimal_fraction(execution.expected_video_duration)
    return (
        str(ffmpeg_executable), "-hide_banner", "-loglevel", "error", "-n",
        "-nostdin", "-i", str(input_video), "-i", str(execution.audio_source),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af",
        f"atrim=duration={duration},asetpts=PTS-STARTPTS", "-c:a", "aac",
        "-video_track_timescale", str(project_media_spec.time_base.denominator),
        "-movflags", "+faststart", str(output_path),
    )


def execute_concat_media(
    execution: ConcatMediaExecutionPlan,
    *,
    project_media_spec: ProjectMediaSpec,
    ffmpeg_executable: str | Path = "ffmpeg",
    command_runner: CommandRunner | None = None,
    media_probe: MediaProbe | None = None,
) -> AdapterResult:
    runner = _run_command if command_runner is None else command_runner
    prober = probe_media if media_probe is None else media_probe
    work_directory: Path | None = None
    succeeded = False
    try:
        _require_execution_and_spec(execution, project_media_spec)
        _validate_input_bindings(execution)
        _ensure_outputs_absent(execution)
        work_directory = Path(
            tempfile.mkdtemp(
                prefix=".concat-attempt-", dir=execution.attempt_directory
            )
        )
        normalized_directory = work_directory / "normalized"
        normalized_directory.mkdir()
        actual_inputs: list[Path] = []
        segment_starts: list[tuple[int, Fraction]] = []
        segment_frame_identities: list[tuple[int, int]] = []
        for segment in execution.segments:
            source_map = _read_json_mapping(segment.source_frame_map, "segment frame map")
            expected_frames, source_time_base = _expected_frames(source_map)
            segment_starts.append((expected_frames[0].pts, source_time_base))
            segment_frame_identities.extend(
                (frame.ordinal, frame.pts) for frame in expected_frames
            )
            source_probe = prober(segment.source_video)
            validate_rendered_media(
                source_probe, source_map, expected_source_frames=expected_frames,
                expected_source_time_base=source_time_base,
            )
            actual = segment.source_video
            if segment.needs_normalize:
                actual = normalized_directory / f"{segment.render_order:06d}-{segment.clip_id}.mp4"
                runner(build_normalize_ffmpeg_command(
                    execution, segment, project_media_spec=project_media_spec,
                    ffmpeg_executable=ffmpeg_executable, output_path=actual,
                ))
                normalized_probe = prober(actual)
                validate_rendered_media(
                    normalized_probe, source_map, expected_source_frames=expected_frames,
                    expected_source_time_base=source_time_base,
                )
                _require_media_spec(normalized_probe, project_media_spec, "normalized segment")
            else:
                _require_media_spec(source_probe, project_media_spec, "concat segment")
            actual_inputs.append(actual)
            _validate_input_bindings(execution)

        final_frames, final_time_base = _expected_frames(execution.final_frame_map)
        if any(time_base != final_time_base for _start, time_base in segment_starts):
            raise ValueError("segment frame maps use a different source time base")
        if segment_frame_identities != [
            (frame.ordinal, frame.pts) for frame in final_frames
        ]:
            raise ValueError(
                "segment frame maps do not flatten to the authoritative final map"
            )

        concat_list = work_directory / "concat.txt"
        _write_concat_list(
            concat_list,
            actual_inputs,
            _segment_durations(execution, segment_starts),
        )
        video_only = work_directory / "video_only.mp4"
        runner(build_concat_ffmpeg_command(
            execution, project_media_spec=project_media_spec, concat_list_path=concat_list,
            ffmpeg_executable=ffmpeg_executable, output_path=video_only,
        ))
        _validate_input_bindings(execution)
        source_probe = prober(execution.audio_source)
        bundle_temporary = work_directory / "final_bundle"
        bundle_temporary.mkdir()
        final_temporary = bundle_temporary / "final.mp4"
        if source_probe.audio is None:
            _copy_fsync(video_only, final_temporary)
            audio_policy = "video_only"
        else:
            runner(build_audio_mux_ffmpeg_command(
                execution, project_media_spec=project_media_spec,
                ffmpeg_executable=ffmpeg_executable,
                input_video=video_only, output_path=final_temporary,
            ))
            audio_policy = "original_video"
        _fsync_existing_file(final_temporary)
        _validate_input_bindings(execution)
        frame_map_temporary = bundle_temporary / "final_frame_map.json"
        _write_json_fsync(frame_map_temporary, execution.final_frame_map)
        proof = dict(_validate_output_files(
            execution, project_media_spec=project_media_spec,
            video_path=final_temporary, frame_map_path=frame_map_temporary,
            media_probe=prober, expected_audio=source_probe.audio is not None,
        ))
        _validate_input_bindings(execution)
        proof["audio_policy"] = audio_policy
        proof["source_asset_sha256"] = execution.source_asset_fingerprint
        proof["segment_video_sha256"] = {
            item.clip_id: item.source_video_sha256 for item in execution.segments
        }
        proof["segment_frame_map_sha256"] = {
            item.clip_id: item.source_frame_map_sha256 for item in execution.segments
        }
        proof["video_sha256"] = _sha256_file(final_temporary)
        proof["frame_map_sha256"] = _sha256_file(frame_map_temporary)
        fingerprint = sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        result = AdapterResult.success(
            output_revision=f"concat-{fingerprint[:16]}",
            output_fingerprint=fingerprint,
            outputs={"video": str(execution.final_output), "frame_map": str(execution.final_frame_map_path)},
            progress=(
                AdapterProgress(stage="preparing", message="verified concat inputs"),
                AdapterProgress(stage="running", message="concatenated source-order video"),
                AdapterProgress(stage="validating", message="validated final media"),
            ),
            validation_proof=proof,
        )
        _publish_bundle(
            bundle_temporary,
            execution.final_output.parent,
            expected_video_sha256=str(proof["video_sha256"]),
            expected_frame_map_sha256=str(proof["frame_map_sha256"]),
        )
        succeeded = True
        return result
    except (OSError, RuntimeError, ValueError, TypeError, InvalidMediaContract, json.JSONDecodeError) as exc:
        return AdapterResult.failed(f"concat media execution failed: {exc}")
    finally:
        if succeeded and work_directory is not None and work_directory.is_dir():
            shutil.rmtree(work_directory, ignore_errors=True)


def validate_concat_outputs(
    execution: ConcatMediaExecutionPlan,
    *,
    project_media_spec: ProjectMediaSpec,
    media_probe: MediaProbe | None = None,
    source_media_probe: MediaProbe | None = None,
) -> Mapping[str, object]:
    _require_execution_and_spec(execution, project_media_spec)
    _validate_input_bindings(execution)
    final_prober = probe_media if media_probe is None else media_probe
    source_prober = final_prober if source_media_probe is None else source_media_probe
    source_media = source_prober(execution.audio_source)
    proof = _validate_output_files(
        execution, project_media_spec=project_media_spec,
        video_path=execution.final_output,
        frame_map_path=execution.final_frame_map_path,
        media_probe=final_prober,
        expected_audio=source_media.audio is not None,
    )
    _validate_input_bindings(execution)
    return proof


def _validate_output_files(
    execution: ConcatMediaExecutionPlan,
    *,
    project_media_spec: ProjectMediaSpec,
    video_path: Path,
    frame_map_path: Path,
    media_probe: MediaProbe,
    expected_audio: bool | None,
) -> Mapping[str, object]:
    frame_map = _read_json_mapping(frame_map_path, "final frame map")
    if frame_map != _thaw(execution.final_frame_map):
        raise ValueError("final frame map differs from execution snapshot")
    expected_frames, source_time_base = _expected_frames(frame_map)
    media = media_probe(video_path)
    proof = validate_rendered_media(
        media, frame_map, expected_source_frames=expected_frames,
        expected_source_time_base=source_time_base,
    )
    _require_media_spec(media, project_media_spec, "final video")
    _validate_timing(media, expected_frames, source_time_base, execution.expected_video_duration)
    if expected_audio is True and media.audio is None:
        raise ValueError("final output is missing original-source audio")
    if expected_audio is False and media.audio is not None:
        raise ValueError("video-only source unexpectedly produced audio")
    duration_proof: Mapping[str, object] | None = None
    if media.audio is not None:
        if media.audio.duration_sec is None:
            raise ValueError("final audio duration is unavailable")
        measured = validate_audio_video_duration(
            audio_duration_sec=media.audio.duration_sec,
            video_duration_sec=float(execution.expected_video_duration),
            max_source_frame_duration_sec=float(execution.audio_video_tolerance),
        )
        duration_proof = {
            "audio_duration_sec": measured.audio_duration_sec,
            "video_duration_sec": measured.video_duration_sec,
            "delta_sec": measured.delta_sec,
            "tolerance_sec": measured.tolerance_sec,
        }
    return {
        "rendered_frame_count": proof.rendered_frame_count,
        "output_pts": list(proof.output_pts),
        "source_pts": list(proof.source_pts),
        "source_time_base": _fraction_dict(proof.source_time_base),
        "audio_video_duration": duration_proof,
    }


def _validate_timing(
    media: ProbedMedia,
    expected_frames: tuple[DecodedFrameTimestamp, ...],
    source_time_base: Fraction,
    expected_duration: Fraction,
) -> None:
    actual_deltas = tuple(
        Fraction(right - left) * media.video.time_base
        for left, right in zip(media.video.frame_pts, media.video.frame_pts[1:])
    )
    source_deltas = tuple(
        Fraction(right.pts - left.pts) * source_time_base
        for left, right in zip(expected_frames, expected_frames[1:])
    )
    if actual_deltas != source_deltas:
        raise ValueError("final video timing differs from authoritative source")
    durations = media.video.frame_duration_pts
    if len(durations) != len(expected_frames):
        raise ValueError("final video frame durations are incomplete")
    last_duration = durations[-1]
    if type(last_duration) is int and last_duration > 0:
        actual_duration = Fraction(
            media.video.frame_pts[-1] - media.video.frame_pts[0] + last_duration
        ) * media.video.time_base
    elif last_duration is None and media.video.duration_pts is not None:
        actual_duration = Fraction(media.video.duration_pts) * media.video.time_base
    else:
        raise ValueError("final video frame durations are incomplete")
    if actual_duration != expected_duration:
        raise ValueError("final video duration differs from authoritative source")


def _expected_frames(frame_map: Mapping[str, object]) -> tuple[tuple[DecodedFrameTimestamp, ...], Fraction]:
    raw_time_base = frame_map.get("source_time_base")
    if not isinstance(raw_time_base, Mapping):
        raise ValueError("frame map source time base is missing")
    numerator = raw_time_base.get("numerator")
    denominator = raw_time_base.get("denominator")
    if type(numerator) is not int or type(denominator) is not int or numerator <= 0 or denominator <= 0:
        raise ValueError("frame map source time base is invalid")
    raw_frames = frame_map.get("frames")
    if (
        not isinstance(raw_frames, Sequence)
        or isinstance(raw_frames, (str, bytes, bytearray))
        or not raw_frames
    ):
        raise ValueError("frame map frames are missing")
    frames: list[DecodedFrameTimestamp] = []
    for item in raw_frames:
        if not isinstance(item, Mapping):
            raise ValueError("frame map entry is invalid")
        ordinal = item.get("source_decoded_frame_ordinal")
        pts = item.get("source_pts")
        if type(ordinal) is not int or type(pts) is not int:
            raise ValueError("frame map source identity is invalid")
        frames.append(DecodedFrameTimestamp(ordinal, pts, None, "pts"))
    return tuple(frames), Fraction(numerator, denominator)


def _validate_input_bindings(execution: ConcatMediaExecutionPlan) -> None:
    _require_expected_sha(execution.audio_source, execution.source_asset_fingerprint, "source asset")
    for segment in execution.segments:
        _require_expected_sha(segment.source_video, segment.source_video_sha256, "segment video")
        _require_expected_sha(segment.source_frame_map, segment.source_frame_map_sha256, "segment frame map")


def _require_media_spec(media: ProbedMedia, spec: ProjectMediaSpec, label: str) -> None:
    compatibility = media_compatibility(media.video, spec)
    if not compatibility.compatible:
        raise ValueError(f"{label} differs from the media spec for this project: {', '.join(compatibility.differences)}")


def _segment_durations(
    execution: ConcatMediaExecutionPlan,
    starts: Sequence[tuple[int, Fraction]],
) -> tuple[Fraction, ...]:
    if len(starts) != len(execution.segments) or not starts:
        raise ValueError("concat segment timing snapshots are incomplete")
    time_base = starts[0][1]
    if any(item[1] != time_base for item in starts):
        raise ValueError("concat segment source time bases differ")
    total_pts = execution.expected_video_duration / time_base
    if total_pts.denominator != 1:
        raise ValueError("expected video duration is not integral in source time base")
    source_end = starts[0][0] + total_pts.numerator
    end_points = [item[0] for item in starts[1:]] + [source_end]
    durations = tuple(
        Fraction(end - start) * time_base
        for (start, _), end in zip(starts, end_points)
    )
    if any(value <= 0 for value in durations) or sum(durations, Fraction()) != execution.expected_video_duration:
        raise ValueError("concat segment durations do not cover authoritative video duration")
    return durations


def _write_concat_list(
    path: Path, inputs: Sequence[Path], durations: Sequence[Fraction]
) -> None:
    if len(inputs) != len(durations):
        raise ValueError("concat inputs and durations differ")
    lines = []
    for item, duration in zip(inputs, durations):
        _require_regular_file(item, "concat input")
        lines.append("file '" + str(item).replace("'", "'\\''") + "'")
        lines.append("duration " + _decimal_fraction(duration))
    _write_bytes_fsync(path, ("\n".join(lines) + "\n").encode("utf-8"))


def _write_json_fsync(path: Path, value: Mapping[str, object]) -> None:
    _write_bytes_fsync(path, (json.dumps(_thaw(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _write_bytes_fsync(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_fsync(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def _fsync_existing_file(path: Path) -> None:
    _require_regular_file(path, "concat temporary output")
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _publish_bundle(
    source: Path,
    destination: Path,
    *,
    expected_video_sha256: str,
    expected_frame_map_sha256: str,
) -> None:
    if (
        not source.is_dir()
        or source.is_symlink()
    ):
        raise ValueError("validated concat bundle is incomplete")
    _require_expected_sha(
        source / "final.mp4", expected_video_sha256, "validated concat video"
    )
    _require_expected_sha(
        source / "final_frame_map.json",
        expected_frame_map_sha256,
        "validated concat frame map",
    )
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"concat output bundle already exists: {destination}")
    _fsync_directory_if_supported(source)
    os.replace(source, destination)
    try:
        _fsync_directory_if_supported(destination.parent, suppress_errors=True)
    except OSError:
        # The atomic bundle is already visible. Never report it as unpublished;
        # restart recovery can validate the complete immutable directory.
        pass


def _fsync_directory_if_supported(
    path: Path, *, suppress_errors: bool = False
) -> bool:
    if os.name == "nt":
        return False
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        if not suppress_errors:
            raise
        return False
    return True


def _ensure_outputs_absent(execution: ConcatMediaExecutionPlan) -> None:
    destination = execution.final_output.parent
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"concat attempt output bundle already exists: {destination}"
        )


def _read_json_mapping(path: Path, label: str) -> Mapping[str, object]:
    _require_regular_file(path, label)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _run_command(command: tuple[str, ...]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"FFmpeg concat command failed: {detail}")


def _require_execution_and_spec(execution: ConcatMediaExecutionPlan, spec: ProjectMediaSpec) -> None:
    _require_execution(execution)
    if not isinstance(spec, ProjectMediaSpec):
        raise TypeError("project_media_spec must be a ProjectMediaSpec")
    if project_media_spec_fingerprint(spec) != execution.project_media_spec_fingerprint:
        raise ValueError("project media spec fingerprint differs from execution snapshot")


def _require_execution(execution: ConcatMediaExecutionPlan) -> None:
    if not isinstance(execution, ConcatMediaExecutionPlan):
        raise TypeError("execution must be a ConcatMediaExecutionPlan")


def _require_attempt_output(path: Path, execution: ConcatMediaExecutionPlan, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    attempt = execution.attempt_directory.resolve()
    try:
        path.resolve().relative_to(attempt)
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside the attempt directory") from exc


def _require_regular_file(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute regular file")


def _require_expected_sha(path: Path, expected: str, label: str) -> None:
    _require_regular_file(path, label)
    if _sha256_file(path) != expected:
        raise ValueError(f"{label} fingerprint differs from execution snapshot")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fraction_dict(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _fraction_from_payload(value: object, label: str) -> Fraction:
    if not isinstance(value, Mapping) or set(value) != {"numerator", "denominator"}:
        raise ValueError(f"{label} must be an exact rational")
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if type(numerator) is not int or type(denominator) is not int or numerator < 0 or denominator <= 0:
        raise ValueError(f"{label} must be a non-negative exact rational")
    return Fraction(numerator, denominator)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be explicit")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _sha(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return text


def _absolute_path(value: object, label: str) -> Path:
    path = Path(_text(value, label))
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path


def _decimal_fraction(value: Fraction) -> str:
    return format(value.numerator / value.denominator, ".12g")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
