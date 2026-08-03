from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Mapping

from .adapters import (
    AdapterInputs,
    AdapterProgress,
    AdapterResult,
    WorkflowAdapterRegistry,
)


@dataclass(frozen=True)
class ExistingWorkflowAdapter:
    name: str
    version: str
    srt_requirement: str
    modules: tuple[str, ...]
    output_relative_path: str
    output_key: str = "trajectory"
    requires_physical_mp4: bool = True
    available: bool = True
    unavailable_reason: str | None = None

    def prepare_inputs(self, inputs: AdapterInputs) -> AdapterInputs:
        if self.requires_physical_mp4 and not inputs.video_path.is_file():
            raise FileNotFoundError(
                f"physical MP4 is required: {inputs.video_path}"
            )
        if self.srt_requirement != "none":
            if inputs.srt_path is None or not inputs.srt_path.is_file():
                raise FileNotFoundError(
                    f"physical SRT with {self.srt_requirement} coverage is required"
                )
        inputs.attempt_directory.mkdir(parents=True, exist_ok=True)
        return inputs

    def build_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        commands = self.build_commands(inputs)
        if len(commands) != 1:
            raise ValueError(
                f"{self.name} has {len(commands)} commands; use build_commands"
            )
        return commands[0]

    def build_commands(
        self, inputs: AdapterInputs
    ) -> tuple[tuple[str, ...], ...]:
        if not self.available:
            raise NotImplementedError(self.unavailable_reason or "adapter is unavailable")
        commands: list[tuple[str, ...]] = []
        for module in self.modules:
            if module == "cadscene.cli.run_sfm":
                commands.append(self._sfm_command(inputs))
            elif module == "cadscene.cli.fuse_srt_sfm":
                commands.append(self._srt_fusion_command(inputs))
            elif module == "cadscene.cli.run_pure_rotation":
                commands.append(self._pure_rotation_command(inputs))
            else:  # pragma: no cover - constructor constants are closed
                raise ValueError(f"unsupported existing workflow module: {module}")
        return tuple(commands)

    def validate_outputs(self, inputs: AdapterInputs) -> AdapterResult:
        output = self._find_output(inputs)
        if output is None:
            return AdapterResult.failed(
                f"expected adapter output is missing: {self.output_relative_path}"
            )
        try:
            payload = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return AdapterResult.failed(f"invalid adapter output: {exc}")
        if not isinstance(payload, dict):
            return AdapterResult.failed("adapter output must be a JSON object")
        if self.name == "pure_rotation":
            poses = payload.get("poses")
            if payload.get("trajectory_mode") != "pure_rotation_only" or not poses:
                return AdapterResult.failed("invalid pure-rotation trajectory output")
        elif not payload.get("poses"):
            return AdapterResult.failed("trajectory output contains no poses")
        fingerprint = sha256(output.read_bytes()).hexdigest()
        return AdapterResult.success(
            output_revision=f"{self.name}:{fingerprint[:16]}",
            output_fingerprint=fingerprint,
            outputs={self.output_key: str(output)},
            progress=(
                AdapterProgress(
                    stage="validated",
                    message=f"validated {self.name} trajectory output",
                    fraction=1.0,
                ),
            ),
        )

    def describe_workbench(self) -> Mapping[str, object]:
        return {
            "workflow": self.name,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "trajectory_output": self.output_relative_path,
        }

    def describe_render(self) -> Mapping[str, object]:
        return {
            "workflow": self.name,
            "trajectory_key": self.output_key,
            "available": self.available,
        }

    def _run_root(self, inputs: AdapterInputs) -> Path:
        return inputs.attempt_directory / inputs.project_id / inputs.clip_id

    def _sfm_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        command = [
            sys.executable,
            "-m",
            "cadscene.cli.run_sfm",
            "--dataset",
            inputs.project_id,
            "--run-id",
            inputs.clip_id,
            "--output-root",
            str(inputs.attempt_directory),
            "--video",
            str(inputs.video_path),
            "--start-frame",
            "0",
            "--frame-step",
            "5",
            "--init-min-tri-angle",
            "2",
            "--no-mask",
        ]
        for key in ("backend", "device", "gpu_index"):
            value = inputs.parameters.get(key)
            if value is not None:
                command.extend([f"--{key.replace('_', '-')}", str(value)])
        return tuple(command)

    def _srt_fusion_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        if inputs.srt_path is None:  # guarded by prepare_inputs
            raise FileNotFoundError("physical SRT is required")
        trajectory = self._run_root(inputs) / "02_sfm/camera_trajectory.json"
        command = [
            sys.executable,
            "-m",
            "cadscene.cli.fuse_srt_sfm",
            "--dataset",
            inputs.project_id,
            "--run-id",
            inputs.clip_id,
            "--output-root",
            str(inputs.attempt_directory),
            "--trajectory",
            str(trajectory),
            "--srt",
            str(inputs.srt_path),
            "--video",
            str(inputs.video_path),
        ]
        return tuple(command)

    def _pure_rotation_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        return (
            sys.executable,
            "-m",
            "cadscene.cli.run_pure_rotation",
            "--dataset",
            inputs.project_id,
            "--run-id",
            inputs.clip_id,
            "--output-root",
            str(inputs.attempt_directory),
            "--video",
            str(inputs.video_path),
        )

    def _find_output(self, inputs: AdapterInputs) -> Path | None:
        direct = inputs.attempt_directory / self.output_relative_path
        nested = self._run_root(inputs) / self.output_relative_path
        return next((path for path in (direct, nested) if path.is_file()), None)


def default_workflow_adapters() -> WorkflowAdapterRegistry:
    return WorkflowAdapterRegistry(
        (
            ExistingWorkflowAdapter(
                name="sfm_only",
                version="1",
                srt_requirement="none",
                modules=("cadscene.cli.run_sfm",),
                output_relative_path="02_sfm/camera_trajectory.json",
            ),
            ExistingWorkflowAdapter(
                name="srt_sfm_fused",
                version="1",
                srt_requirement="trajectory",
                modules=(
                    "cadscene.cli.run_sfm",
                    "cadscene.cli.fuse_srt_sfm",
                ),
                output_relative_path="02_fusion/camera_trajectory_fused.json",
            ),
            ExistingWorkflowAdapter(
                name="srt_full_pose",
                version="1",
                srt_requirement="full_pose",
                modules=(),
                output_relative_path="camera_trajectory_full_pose.json",
                available=False,
                unavailable_reason=(
                    "srt_full_pose is interface-only; no executable workflow exists"
                ),
            ),
            ExistingWorkflowAdapter(
                name="pure_rotation",
                version="1",
                srt_requirement="none",
                modules=("cadscene.cli.run_pure_rotation",),
                output_relative_path="02_pure_rotation/camera_rotation_raw.json",
            ),
        )
    )
