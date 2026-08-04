from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from cadscene.video_analysis.pts import DecodedFrameTimestamp

from .adapters import AdapterResult
from .identifiers import is_safe_stable_id, validate_project_id
from .media import ProjectMediaSpec


@dataclass(frozen=True)
class RenderInputs:
    """Validated, manifest-free inputs for one immutable clip render attempt."""

    project_id: str
    clip_id: str
    workflow: str
    physical_video_path: Path
    authoritative_frame_map_path: Path
    authoritative_source_frames: tuple[DecodedFrameTimestamp, ...]
    source_time_base: Fraction
    workbench_artifact_path: Path
    workbench_output_revision: str
    workbench_output_fingerprint: str
    attempt_directory: Path
    project_media_spec: ProjectMediaSpec
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid clip_id")
        if not isinstance(self.workflow, str) or not self.workflow.strip():
            raise ValueError("workflow must not be empty")
        if self.workflow != self.workflow.strip():
            raise ValueError("workflow must not contain surrounding whitespace")
        for name, must_exist in (
            ("physical_video_path", True),
            ("authoritative_frame_map_path", True),
            ("workbench_artifact_path", True),
            ("attempt_directory", False),
        ):
            value = getattr(self, name)
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{name} must be an absolute path")
            if must_exist and not value.is_file():
                label = name.removesuffix("_path").replace("_", " ")
                raise ValueError(f"{label} must be an existing file")
            if not must_exist and value.exists() and not value.is_dir():
                raise ValueError("attempt_directory must be a directory")
        frames = self.authoritative_source_frames
        if (
            not isinstance(frames, Sequence)
            or isinstance(frames, (str, bytes, bytearray))
            or not frames
            or any(not isinstance(frame, DecodedFrameTimestamp) for frame in frames)
        ):
            raise ValueError(
                "authoritative source frames must be a non-empty sequence"
            )
        normalized_frames = tuple(frames)
        for previous, current in zip(normalized_frames, normalized_frames[1:]):
            if current.ordinal != previous.ordinal + 1 or current.pts <= previous.pts:
                raise ValueError(
                    "authoritative source frames must be contiguous and ordered"
                )
        if (
            not isinstance(self.source_time_base, Fraction)
            or self.source_time_base <= 0
        ):
            raise ValueError("source_time_base must be a positive Fraction")
        if (
            not isinstance(self.workbench_output_revision, str)
            or not self.workbench_output_revision.strip()
        ):
            raise ValueError("workbench output revision must not be empty")
        fingerprint = self.workbench_output_fingerprint
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("workbench output fingerprint must be lowercase SHA-256")
        if not isinstance(self.project_media_spec, ProjectMediaSpec):
            raise TypeError("project_media_spec must be a ProjectMediaSpec")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be a mapping")
        object.__setattr__(self, "authoritative_source_frames", normalized_frames)
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class RenderExecutionPlan:
    """Commands plus a checked structured validator, with no persistence access."""

    commands: tuple[tuple[str, ...], ...]
    validate: Callable[[], AdapterResult]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.commands, tuple)
            or not self.commands
            or any(
                not isinstance(command, tuple)
                or not command
                or any(
                    not isinstance(token, str) or not token
                    for token in command
                )
                for command in self.commands
            )
        ):
            raise ValueError("commands must contain non-empty string token tuples")
        if not callable(self.validate):
            raise TypeError("validate must be callable")
        validator = self.validate

        def checked_validate() -> AdapterResult:
            result = validator()
            if not isinstance(result, AdapterResult):
                raise TypeError("render validator must return AdapterResult")
            return result

        object.__setattr__(self, "validate", checked_validate)


class RenderAdapter(Protocol):
    workflow: str
    name: str
    version: str

    def prepare(self, inputs: RenderInputs) -> RenderExecutionPlan: ...


class RenderAdapterRegistry:
    """Fail-closed workflow routing for manifest-free render adapters."""

    def __init__(self, adapters: Sequence[RenderAdapter]) -> None:
        by_workflow: dict[str, RenderAdapter] = {}
        identities: set[tuple[str, str]] = set()
        for adapter in adapters:
            workflow = getattr(adapter, "workflow", None)
            name = getattr(adapter, "name", None)
            version = getattr(adapter, "version", None)
            if not all(
                isinstance(value, str) and value and value == value.strip()
                for value in (workflow, name, version)
            ):
                raise ValueError(
                    "render adapter workflow, name, and version must be explicit"
                )
            if workflow in by_workflow:
                raise ValueError(f"render adapter workflow must be unique: {workflow}")
            identity = (name, version)
            if identity in identities:
                raise ValueError(
                    f"render adapter name/version must be unique: {name}@{version}"
                )
            if not callable(getattr(adapter, "prepare", None)):
                raise TypeError("render adapter prepare must be callable")
            by_workflow[workflow] = adapter
            identities.add(identity)
        self._by_workflow = MappingProxyType(by_workflow)

    def for_workflow(self, workflow: str) -> RenderAdapter:
        try:
            return self._by_workflow[workflow]
        except KeyError as exc:
            raise KeyError(f"unsupported render workflow: {workflow}") from exc

    def workflows(self) -> tuple[str, ...]:
        return tuple(self._by_workflow)
