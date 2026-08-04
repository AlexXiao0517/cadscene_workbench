from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class AdapterProgress:
    stage: str
    message: str
    fraction: float | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("stage must not be empty")
        if not self.message:
            raise ValueError("message must not be empty")
        if self.fraction is not None and not 0.0 <= self.fraction <= 1.0:
            raise ValueError("fraction must be between zero and one")

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "stage": self.stage,
            "message": self.message,
        }
        if self.fraction is not None:
            value["fraction"] = self.fraction
        return value


@dataclass(frozen=True)
class AdapterInputs:
    project_id: str
    clip_id: str
    video_path: Path
    srt_path: Path | None
    attempt_directory: Path
    parameters: Mapping[str, object] = field(default_factory=dict)
    source_start_pts: int | None = None
    source_end_pts_exclusive: int | None = None
    source_time_base: Fraction | None = None
    frame_map_path: Path | None = None


@dataclass(frozen=True)
class AdapterResult:
    status: str
    output_revision: str | None = None
    output_fingerprint: str | None = None
    outputs: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None
    progress: tuple[AdapterProgress, ...] = ()

    @classmethod
    def success(
        cls,
        *,
        output_revision: str,
        output_fingerprint: str,
        outputs: Mapping[str, str],
        progress: Sequence[AdapterProgress] = (),
    ) -> AdapterResult:
        return cls(
            status="success",
            output_revision=output_revision,
            output_fingerprint=output_fingerprint,
            outputs=dict(outputs),
            progress=tuple(progress),
        )

    @classmethod
    def failed(cls, error: str) -> AdapterResult:
        return cls(status="failed", error=error)


class WorkflowAdapter(Protocol):
    name: str
    version: str
    requires_physical_mp4: bool
    srt_requirement: str
    available: bool
    unavailable_reason: str | None

    def prepare_inputs(self, inputs: AdapterInputs) -> AdapterInputs: ...

    def build_command(self, inputs: AdapterInputs) -> tuple[str, ...]: ...

    def build_commands(
        self, inputs: AdapterInputs
    ) -> tuple[tuple[str, ...], ...]: ...

    def validate_outputs(self, inputs: AdapterInputs) -> AdapterResult: ...

    def describe_workbench(self) -> Mapping[str, object]: ...

    def describe_render(self) -> Mapping[str, object]: ...


class RenderAdapter(Protocol):
    """Manifest-free render boundary reserved for later render orchestration."""

    name: str
    version: str

    def validate_outputs(self, inputs: AdapterInputs) -> AdapterResult: ...


class WorkflowAdapterRegistry:
    def __init__(self, adapters: Sequence[WorkflowAdapter]) -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}
        if len(self._adapters) != len(adapters):
            raise ValueError("workflow adapter names must be unique")

    def for_workflow(self, workflow: str) -> WorkflowAdapter:
        try:
            return self._adapters[workflow]
        except KeyError as exc:
            raise KeyError(f"unsupported workflow: {workflow}") from exc

    def workflows(self) -> tuple[str, ...]:
        return tuple(self._adapters)
