from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
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
from cadscene.workflow.job_runner import resolve_sfm_python


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
    pure_rotation_backend_root: Path | None = None
    pure_rotation_backend_command: tuple[str, ...] | None = None
    pure_rotation_calibration_root: Path | None = None

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
        if self.name == "srt_sfm_fused":
            _validate_exact_clip_mapping(inputs)
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
                if self.pure_rotation_calibration_root is not None:
                    commands.append(self._pure_rotation_calibration_command(inputs))
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
            str(resolve_sfm_python()),
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
            "--progress-file",
            str(inputs.attempt_directory / "adapter_progress.json"),
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
            "--time-offset-sec",
            _source_offset_seconds(inputs),
        ]
        return tuple(command)

    def _pure_rotation_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        command = [
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
        ]
        if self.pure_rotation_backend_root is not None:
            command.extend(
                ["--backend-root", str(self.pure_rotation_backend_root)]
            )
        if self.pure_rotation_backend_command is not None:
            command.extend(
                [
                    "--backend-command-json",
                    json.dumps(list(self.pure_rotation_backend_command)),
                ]
            )
        if self.pure_rotation_calibration_root is not None:
            command.extend(
                [
                    "--cadscene-readonly",
                    str(inputs.attempt_directory / "pure_rotation_calibration"),
                ]
            )
        return tuple(command)

    def _pure_rotation_calibration_command(
        self, inputs: AdapterInputs
    ) -> tuple[str, ...]:
        if (
            self.pure_rotation_backend_root is None
            or self.pure_rotation_backend_command is None
            or self.pure_rotation_calibration_root is None
        ):
            raise ValueError("pure-rotation calibration configuration is incomplete")
        return (
            self.pure_rotation_backend_command[0],
            "-m",
            "cadscene.cli.prepare_pure_rotation_calibration",
            "--backend-root",
            str(self.pure_rotation_backend_root),
            "--source-root",
            str(self.pure_rotation_calibration_root),
            "--video",
            str(inputs.video_path),
            "--output-dir",
            str(inputs.attempt_directory / "pure_rotation_calibration"),
        )

    def _find_output(self, inputs: AdapterInputs) -> Path | None:
        direct = inputs.attempt_directory / self.output_relative_path
        nested = self._run_root(inputs) / self.output_relative_path
        return next((path for path in (direct, nested) if path.is_file()), None)


def default_workflow_adapters(
    *,
    pure_rotation_backend_root: Path | None = None,
    pure_rotation_backend_command: tuple[str, ...] | None = None,
    pure_rotation_calibration_root: Path | None = None,
) -> WorkflowAdapterRegistry:
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
                pure_rotation_backend_root=pure_rotation_backend_root,
                pure_rotation_backend_command=pure_rotation_backend_command,
                pure_rotation_calibration_root=pure_rotation_calibration_root,
            ),
        )
    )


def _validate_exact_clip_mapping(inputs: AdapterInputs) -> None:
    if (
        inputs.source_start_pts is None
        or inputs.source_end_pts_exclusive is None
        or inputs.source_time_base is None
        or inputs.frame_map_path is None
    ):
        raise ValueError(
            "srt_sfm_fused requires authoritative PTS interval and frame map"
        )
    if not inputs.frame_map_path.is_file():
        raise FileNotFoundError(f"clip frame map is missing: {inputs.frame_map_path}")
    payload = json.loads(inputs.frame_map_path.read_text(encoding="utf-8"))
    time_base = payload.get("source_time_base") or {}
    mapped_time_base = Fraction(
        int(time_base.get("numerator")), int(time_base.get("denominator"))
    )
    if mapped_time_base != inputs.source_time_base:
        raise ValueError("frame map time base disagrees with authoritative interval")
    mapped = next(
        (
            item
            for item in payload.get("clips", ())
            if item.get("clip_id") == inputs.clip_id
        ),
        None,
    )
    if mapped is None:
        raise ValueError("frame map does not contain the requested clip_id")
    if (
        int(mapped.get("source_start_pts")) != inputs.source_start_pts
        or int(mapped.get("source_end_pts_exclusive"))
        != inputs.source_end_pts_exclusive
    ):
        raise ValueError("frame map clip interval disagrees with authoritative interval")
    frames = mapped.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("frame map clip interval contains no decoded frames")
    points = [int(item["pts"]) for item in frames]
    if points[0] != inputs.source_start_pts:
        raise ValueError("frame map first frame disagrees with clip interval")
    if any(
        point < inputs.source_start_pts
        or point >= inputs.source_end_pts_exclusive
        for point in points
    ) or any(right <= left for left, right in zip(points, points[1:])):
        raise ValueError("frame map last/order disagrees with clip interval")


def _source_offset_seconds(inputs: AdapterInputs) -> str:
    if inputs.source_start_pts is None or inputs.source_time_base is None:
        raise ValueError("source offset requires authoritative integer PTS")
    offset = Fraction(inputs.source_start_pts) * inputs.source_time_base
    with localcontext() as context:
        context.prec = 30
        decimal = Decimal(offset.numerator) / Decimal(offset.denominator)
    rendered = format(decimal, "f").rstrip("0").rstrip(".")
    return rendered or "0"
