from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from cadscene.video_analysis.pts import DecodedFrameIndex

from .adapters import AdapterResult
from .concat import ConcatPlan, build_final_frame_map
from .media import ProjectMediaSpec


@dataclass(frozen=True)
class ConcatMediaInputs:
    """Manifest-free inputs for one immutable project concat attempt."""

    plan: ConcatPlan
    source_frame_index: DecodedFrameIndex
    source_video_path: Path
    attempt_directory: Path
    project_media_spec: ProjectMediaSpec

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ConcatPlan):
            raise TypeError("plan must be a ConcatPlan")
        if not isinstance(self.source_frame_index, DecodedFrameIndex):
            raise TypeError("source_frame_index must be a DecodedFrameIndex")
        _require_regular_file(self.source_video_path, "source video")
        attempt = self.attempt_directory
        if (
            not isinstance(attempt, Path)
            or not attempt.is_absolute()
            or attempt.is_symlink()
            or not attempt.is_dir()
        ):
            raise ValueError("attempt directory must be an existing absolute directory")
        if not isinstance(self.project_media_spec, ProjectMediaSpec):
            raise TypeError("project_media_spec must be a ProjectMediaSpec")


@dataclass(frozen=True)
class ConcatSegmentExecution:
    clip_id: str
    render_order: int
    source_video: Path
    source_frame_map: Path
    source_video_sha256: str
    source_frame_map_sha256: str
    concat_input: Path
    needs_normalize: bool
    input_output_revision: str
    input_output_fingerprint: str
    input_proof_fingerprint: str
    input_publication_operation_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "render_order": self.render_order,
            "source_video": str(self.source_video),
            "source_frame_map": str(self.source_frame_map),
            "source_video_sha256": self.source_video_sha256,
            "source_frame_map_sha256": self.source_frame_map_sha256,
            "concat_input": str(self.concat_input),
            "needs_normalize": self.needs_normalize,
            "input_output_revision": self.input_output_revision,
            "input_output_fingerprint": self.input_output_fingerprint,
            "input_proof_fingerprint": self.input_proof_fingerprint,
            "input_publication_operation_id": self.input_publication_operation_id,
        }


@dataclass(frozen=True)
class ConcatMediaExecutionPlan:
    project_id: str
    project_revision: int
    clips_revision: int
    project_media_spec_revision: str
    project_media_spec_fingerprint: str
    source_asset_fingerprint: str
    segments: tuple[ConcatSegmentExecution, ...]
    audio_source: Path
    attempt_directory: Path
    video_only_output: Path
    final_output: Path
    final_frame_map_path: Path
    final_frame_map: Mapping[str, object]
    expected_video_duration: Fraction
    audio_video_tolerance: Fraction
    validate: Callable[[], AdapterResult]

    def __post_init__(self) -> None:
        validator = self.validate
        if not callable(validator):
            raise TypeError("concat validator must be callable")
        _required_sha(
            self.project_media_spec_fingerprint,
            "project media spec fingerprint",
        )
        _required_sha(self.source_asset_fingerprint, "source asset fingerprint")
        if (
            not isinstance(self.segments, Sequence)
            or isinstance(self.segments, (str, bytes, bytearray))
            or not self.segments
            or any(
                not isinstance(item, ConcatSegmentExecution)
                for item in self.segments
            )
        ):
            raise ValueError("concat execution segments must be explicit")
        object.__setattr__(self, "segments", tuple(self.segments))
        if [item.render_order for item in self.segments] != list(
            range(len(self.segments))
        ):
            raise ValueError("concat execution render_order must be contiguous")
        for name in (
            "audio_source",
            "attempt_directory",
            "video_only_output",
            "final_output",
            "final_frame_map_path",
        ):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        attempt_directory = self.attempt_directory
        if attempt_directory.is_symlink() or not attempt_directory.is_dir():
            raise ValueError(
                "attempt_directory must be an existing regular directory"
            )
        bundle_directory = self.final_output.parent
        if (
            self.video_only_output.parent != attempt_directory
            or bundle_directory.parent != attempt_directory
            or self.final_frame_map_path.parent != bundle_directory
        ):
            raise ValueError("concat outputs must remain in the attempt directory")
        if len(
            {self.video_only_output, self.final_output, self.final_frame_map_path}
        ) != 3:
            raise ValueError("concat attempt outputs must be distinct")
        if not isinstance(self.final_frame_map, Mapping):
            raise TypeError("final frame map must be a mapping")
        object.__setattr__(
            self, "final_frame_map", _freeze_mapping(self.final_frame_map)
        )
        if (
            not isinstance(self.expected_video_duration, Fraction)
            or self.expected_video_duration <= 0
            or not isinstance(self.audio_video_tolerance, Fraction)
            or self.audio_video_tolerance <= 0
        ):
            raise ValueError("concat durations must be positive exact rationals")

        def checked_validate() -> AdapterResult:
            result = validator()
            if not isinstance(result, AdapterResult):
                raise TypeError("concat validator must return AdapterResult")
            return result

        object.__setattr__(self, "validate", checked_validate)

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "clips_revision": self.clips_revision,
            "project_media_spec_revision": self.project_media_spec_revision,
            "project_media_spec_fingerprint": self.project_media_spec_fingerprint,
            "source_asset_fingerprint": self.source_asset_fingerprint,
            "segments": [item.to_dict() for item in self.segments],
            "audio_source": str(self.audio_source),
            "audio_policy": "original_video",
            "attempt_directory": str(self.attempt_directory),
            "video_only_output": str(self.video_only_output),
            "final_output": str(self.final_output),
            "final_frame_map_path": str(self.final_frame_map_path),
            "final_frame_map": _thaw(self.final_frame_map),
            "expected_video_duration": _fraction_dict(self.expected_video_duration),
            "audio_video_tolerance": _fraction_dict(self.audio_video_tolerance),
        }


class ConcatMediaAdapter:
    """Builds a checked execution boundary without reading project manifests."""

    name = "project_concat"
    version = "1"

    def __init__(
        self,
        *,
        validator: Callable[[ConcatMediaInputs, ConcatMediaExecutionPlan], AdapterResult]
        | None = None,
    ) -> None:
        self._validator = validator

    def prepare(self, inputs: ConcatMediaInputs) -> ConcatMediaExecutionPlan:
        if not isinstance(inputs, ConcatMediaInputs):
            raise TypeError("inputs must be ConcatMediaInputs")
        plan = inputs.plan
        if Path(plan.audio_source) != inputs.source_video_path:
            raise ValueError("audio source path differs from the authoritative source")
        final_map = build_final_frame_map(plan, inputs.source_frame_index)
        _validate_plan_input_bindings(inputs)
        normalized_root = inputs.attempt_directory / "normalized"
        segments: list[ConcatSegmentExecution] = []
        for expected_order, entry in enumerate(plan.entries):
            if entry.render_order != expected_order:
                raise ValueError("concat plan render_order must be contiguous and ordered")
            if not entry.ready or entry.dependency_required:
                raise ValueError(
                    f"concat entry {entry.clip_id} is not ready or still requires a dependency"
                )
            if type(entry.needs_normalize) is not bool:
                raise ValueError("concat entry needs_normalize must be explicit")
            video = _required_entry_path(entry.input_video_path, "input video")
            frame_map = _required_entry_path(entry.input_frame_map_path, "input frame map")
            output_revision = _required_text(
                entry.input_output_revision, "input output revision"
            )
            output_fingerprint = _required_sha(
                entry.input_output_fingerprint, "input output fingerprint"
            )
            proof_fingerprint = _required_sha(
                entry.input_proof_fingerprint, "input proof fingerprint"
            )
            operation_id = _required_text(
                entry.input_publication_operation_id,
                "input publication operation id",
            )
            normalize = entry.needs_normalize is True
            concat_input = (
                normalized_root / f"{entry.render_order:06d}-{entry.clip_id}.mp4"
                if normalize
                else video
            )
            segments.append(
                ConcatSegmentExecution(
                    clip_id=entry.clip_id,
                    render_order=entry.render_order,
                    source_video=video,
                    source_frame_map=frame_map,
                    source_video_sha256=_required_sha(
                        entry.input_video_sha256, "input video SHA-256"
                    ),
                    source_frame_map_sha256=_required_sha(
                        entry.input_frame_map_sha256, "input frame map SHA-256"
                    ),
                    concat_input=concat_input,
                    needs_normalize=normalize,
                    input_output_revision=output_revision,
                    input_output_fingerprint=output_fingerprint,
                    input_proof_fingerprint=proof_fingerprint,
                    input_publication_operation_id=operation_id,
                )
            )
        expected_duration = Fraction(
            inputs.source_frame_index.source_end_pts_exclusive
            - inputs.source_frame_index.source_start_pts
        ) * inputs.source_frame_index.time_base
        max_duration = max(
            _frame_duration_seconds(inputs.source_frame_index, ordinal)
            for ordinal in range(len(inputs.source_frame_index.frames))
        )
        tolerance = max(Fraction(1, 20), max_duration)
        validator = self._validator
        holder: dict[str, ConcatMediaExecutionPlan] = {}

        def validate() -> AdapterResult:
            _validate_execution_bindings(holder["plan"])
            if validator is None:
                return AdapterResult.failed("concat output validator is not configured")
            result = validator(inputs, holder["plan"])
            _validate_execution_bindings(holder["plan"])
            return result

        bundle_directory = inputs.attempt_directory / "final_bundle"
        execution = ConcatMediaExecutionPlan(
            project_id=plan.project_id,
            project_revision=plan.project_revision,
            clips_revision=plan.clips_revision,
            project_media_spec_revision=plan.project_media_spec_revision,
            project_media_spec_fingerprint=project_media_spec_fingerprint(
                inputs.project_media_spec
            ),
            source_asset_fingerprint=plan.source_asset_fingerprint,
            segments=tuple(segments),
            audio_source=inputs.source_video_path,
            attempt_directory=inputs.attempt_directory,
            video_only_output=inputs.attempt_directory / "video_only.mp4",
            final_output=bundle_directory / "final.mp4",
            final_frame_map_path=bundle_directory / "final_frame_map.json",
            final_frame_map=_freeze_mapping(final_map),
            expected_video_duration=expected_duration,
            audio_video_tolerance=tolerance,
            validate=validate,
        )
        holder["plan"] = execution
        return execution


def _frame_duration_seconds(index: DecodedFrameIndex, ordinal: int) -> Fraction:
    frame = index.frames[ordinal]
    end_pts = (
        index.frames[ordinal + 1].pts
        if ordinal + 1 < len(index.frames)
        else index.source_end_pts_exclusive
    )
    return Fraction(end_pts - frame.pts) * index.time_base


def project_media_spec_fingerprint(spec: ProjectMediaSpec) -> str:
    if not isinstance(spec, ProjectMediaSpec):
        raise TypeError("project media spec must be a ProjectMediaSpec")
    payload = json.dumps(
        spec.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _validate_plan_input_bindings(inputs: ConcatMediaInputs) -> None:
    plan = inputs.plan
    _require_regular_file(inputs.source_video_path, "source video")
    if _sha256_file(inputs.source_video_path) != plan.source_asset_fingerprint:
        raise ValueError("source asset fingerprint differs from the concat plan")
    for entry in plan.entries:
        video = _required_entry_path(entry.input_video_path, "input video")
        frame_map = _required_entry_path(entry.input_frame_map_path, "input frame map")
        _require_expected_sha(video, entry.input_video_sha256, "input video")
        _require_expected_sha(frame_map, entry.input_frame_map_sha256, "input frame map")


def _validate_execution_bindings(execution: ConcatMediaExecutionPlan) -> None:
    _require_regular_file(execution.audio_source, "source video")
    _require_expected_sha(
        execution.audio_source,
        execution.source_asset_fingerprint,
        "source asset",
    )
    for segment in execution.segments:
        _require_expected_sha(
            segment.source_video,
            segment.source_video_sha256,
            "input video",
        )
        _require_expected_sha(
            segment.source_frame_map,
            segment.source_frame_map_sha256,
            "input frame map",
        )


def _required_entry_path(value: str | None, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} path is missing")
    path = Path(value)
    _require_regular_file(path, label)
    return path


def _require_regular_file(path: Path, label: str) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ValueError(f"{label} must be an existing absolute regular file")


def _require_expected_sha(path: Path, expected: str | None, label: str) -> None:
    _require_regular_file(path, label)
    digest = _required_sha(expected, f"{label} SHA-256")
    if _sha256_file(path) != digest:
        raise ValueError(f"{label} fingerprint differs from the concat plan")


def _required_sha(value: str | None, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _required_text(value: str | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be explicit")
    return value


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fraction_dict(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})


def _freeze(value: object) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value
