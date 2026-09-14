from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from hashlib import sha256
import json
import math
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
from cadscene.sfm.resolution import (
    normalize_reconstruction_resolution,
    reconstruction_dimensions,
    target_height_for_resolution,
)
from cadscene.srt.fixed_track_visual_pose import FixedTrackVisualPoseConfig
from cadscene.srt.full_pose import FullPoseBuildConfig


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
        if self.name in {
            "srt_sfm_fused",
            "srt_full_pose",
            "srt_fixed_track_visual_pose",
        }:
            _validate_exact_clip_mapping(inputs)
        if self.name == "sfm_only":
            if inputs.frame_map_path is None or not inputs.frame_map_path.is_file():
                raise FileNotFoundError("SfM solve frame map is required")
            if (
                inputs.core_frame_map_path is None
                or not inputs.core_frame_map_path.is_file()
            ):
                raise FileNotFoundError("SfM core frame map is required")
        inputs.attempt_directory.mkdir(parents=True, exist_ok=True)
        if self.name == "srt_full_pose":
            _write_full_pose_config(inputs)
        if self.name == "srt_fixed_track_visual_pose":
            _write_fixed_track_config(inputs)
            _fixed_track_reconstruction_paths(inputs)
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
            if (
                self.name == "srt_fixed_track_visual_pose"
                and _has_reconstruction_reuse(inputs)
                and module
                in {
                    "cadscene.cli.plan_srt_adaptive_frames",
                    "cadscene.cli.run_sfm",
                }
            ):
                continue
            if module == "cadscene.cli.run_sfm":
                commands.append(self._sfm_command(inputs))
            elif module == "cadscene.cli.partition_sfm_trajectory":
                commands.append(self._sfm_partition_command(inputs))
            elif module == "cadscene.cli.fuse_srt_sfm":
                commands.append(self._srt_fusion_command(inputs))
            elif module == "cadscene.cli.run_pure_rotation":
                if self.pure_rotation_calibration_root is not None:
                    commands.append(self._pure_rotation_calibration_command(inputs))
                commands.append(self._pure_rotation_command(inputs))
            elif module == "cadscene.cli.build_srt_full_pose":
                commands.append(self._full_pose_command(inputs))
            elif module == "cadscene.cli.build_srt_fixed_track_visual_pose":
                commands.append(self._fixed_track_command(inputs))
            elif module == "cadscene.cli.plan_srt_adaptive_frames":
                commands.append(self._fixed_track_plan_command(inputs))
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
        elif self.name == "srt_full_pose":
            meta = payload.get("meta")
            if (
                not payload.get("poses")
                or not isinstance(meta, Mapping)
                or meta.get("trajectory_mode") != "srt_full_pose"
                or meta.get("coordinate_system") != "cad_local_m"
                or meta.get("metric_scale_locked") is not True
            ):
                return AdapterResult.failed("invalid full-pose trajectory output")
        elif self.name == "srt_fixed_track_visual_pose":
            meta = payload.get("meta")
            if (
                not payload.get("poses")
                or not isinstance(meta, Mapping)
                or meta.get("trajectory_mode") != "srt_fixed_track_visual_pose"
                or meta.get("coordinate_system") != "cad_local_m"
                or meta.get("metric_scale_locked") is not True
                or meta.get("position_source") != "srt_cad_locked"
            ):
                return AdapterResult.failed("invalid fixed-track visual-pose output")
        elif not payload.get("poses"):
            return AdapterResult.failed("trajectory output contains no poses")
        outputs = {self.output_key: str(output)}
        validation_proof: dict[str, object] | None = None
        digest = sha256(output.read_bytes())
        if self.name == "sfm_only":
            solve_output = next(
                (
                    path
                    for path in (
                        inputs.attempt_directory
                        / "02_sfm/camera_trajectory_solve.json",
                        self._run_root(inputs)
                        / "02_sfm/camera_trajectory_solve.json",
                    )
                    if path.is_file()
                ),
                None,
            )
            if solve_output is None:
                return AdapterResult.failed("SfM solve trajectory output is missing")
            if inputs.frame_map_path is None or inputs.core_frame_map_path is None:
                return AdapterResult.failed("SfM frame-map bindings are missing")
            for path in (
                solve_output,
                inputs.frame_map_path,
                inputs.core_frame_map_path,
            ):
                digest.update(path.read_bytes())
            outputs.update(
                {
                    "solve_trajectory": str(solve_output),
                    "solve_video": str(inputs.video_path),
                    "solve_frame_map": str(inputs.frame_map_path),
                    "core_frame_map": str(inputs.core_frame_map_path),
                }
            )
            validation_proof = {
                "trajectory_sha256": sha256(output.read_bytes()).hexdigest(),
                "solve_trajectory_sha256": sha256(
                    solve_output.read_bytes()
                ).hexdigest(),
                "solve_frame_map_sha256": sha256(
                    inputs.frame_map_path.read_bytes()
                ).hexdigest(),
                "core_frame_map_sha256": sha256(
                    inputs.core_frame_map_path.read_bytes()
                ).hexdigest(),
            }
        elif self.name == "srt_full_pose":
            root = output.parent
            run_root = root.parent
            artifacts = {
                "diagnostics": root / "georeference_diagnostics.json",
                "camera_path": root / "camera_path_full_pose.csv",
                "report": root / "full_pose_report.md",
                "initial_camera_track": run_root
                / "03_alignment/camera_track_pred.json",
                "viewer_scene": run_root
                / "05_viewer_scene/sfm_viewer_scene.json",
            }
            if int(self.version) >= 3:
                artifacts.update({
                    "calibration": root / "camera_calibration.json",
                    "terrain_context": root / "terrain_context.json",
                    "terrain_controls": root / "terrain_controls.npz",
                })
            missing = [key for key, path in artifacts.items() if not path.is_file()]
            if missing:
                return AdapterResult.failed(
                    "full-pose artifact output is missing: " + ", ".join(missing)
                )
            for path in artifacts.values():
                digest.update(path.read_bytes())
            if inputs.frame_map_path is None:
                return AdapterResult.failed("full-pose frame-map binding is missing")
            config_path = _full_pose_config_path(inputs)
            for path in (inputs.frame_map_path, config_path):
                digest.update(path.read_bytes())
            outputs.update({key: str(path) for key, path in artifacts.items()})
            validation_proof = {
                "trajectory_sha256": sha256(output.read_bytes()).hexdigest(),
                "diagnostics_sha256": sha256(
                    artifacts["diagnostics"].read_bytes()
                ).hexdigest(),
                "camera_path_sha256": sha256(
                    artifacts["camera_path"].read_bytes()
                ).hexdigest(),
                "report_sha256": sha256(artifacts["report"].read_bytes()).hexdigest(),
                "initial_camera_track_sha256": sha256(
                    artifacts["initial_camera_track"].read_bytes()
                ).hexdigest(),
                "viewer_scene_sha256": sha256(
                    artifacts["viewer_scene"].read_bytes()
                ).hexdigest(),
                "frame_map_sha256": sha256(
                    inputs.frame_map_path.read_bytes()
                ).hexdigest(),
                "configuration_sha256": sha256(config_path.read_bytes()).hexdigest(),
                "metric_scale_locked": True,
            }
            for key in ("calibration", "terrain_context", "terrain_controls"):
                if key in artifacts:
                    validation_proof[f"{key}_sha256"] = sha256(artifacts[key].read_bytes()).hexdigest()
        elif self.name == "srt_fixed_track_visual_pose":
            root = output.parent
            run_root = root.parent
            artifacts = {
                "diagnostics": root / "orientation_diagnostics.json",
                "camera_path": root / "camera_path_srt_locked.csv",
                "report": root / "visual_pose_report.md",
                "calibration": root / "camera_calibration.json",
                "joint_alignment": root / "joint_alignment.json",
                "terrain_context": root / "terrain_context.json",
                "terrain_controls": root / "terrain_controls.npz",
                "initial_camera_track": run_root
                / "03_alignment/camera_track_pred.json",
                "viewer_scene": run_root
                / "05_viewer_scene/sfm_viewer_scene.json",
            }
            missing = [key for key, path in artifacts.items() if not path.is_file()]
            if missing:
                return AdapterResult.failed(
                    "fixed-track artifact output is missing: " + ", ".join(missing)
                )
            if inputs.frame_map_path is None:
                return AdapterResult.failed("fixed-track frame-map binding is missing")
            config_path = _fixed_track_config_path(inputs)
            for path in (*artifacts.values(), inputs.frame_map_path, config_path):
                digest.update(path.read_bytes())
            outputs.update({key: str(path) for key, path in artifacts.items()})
            validation_proof = {
                "trajectory_sha256": sha256(output.read_bytes()).hexdigest(),
                "diagnostics_sha256": sha256(
                    artifacts["diagnostics"].read_bytes()
                ).hexdigest(),
                "camera_path_sha256": sha256(
                    artifacts["camera_path"].read_bytes()
                ).hexdigest(),
                "report_sha256": sha256(artifacts["report"].read_bytes()).hexdigest(),
                "initial_camera_track_sha256": sha256(
                    artifacts["initial_camera_track"].read_bytes()
                ).hexdigest(),
                "viewer_scene_sha256": sha256(
                    artifacts["viewer_scene"].read_bytes()
                ).hexdigest(),
                "calibration_sha256": sha256(
                    artifacts["calibration"].read_bytes()
                ).hexdigest(),
                "joint_alignment_sha256": sha256(
                    artifacts["joint_alignment"].read_bytes()
                ).hexdigest(),
                "terrain_context_sha256": sha256(
                    artifacts["terrain_context"].read_bytes()
                ).hexdigest(),
                "terrain_controls_sha256": sha256(
                    artifacts["terrain_controls"].read_bytes()
                ).hexdigest(),
                "frame_map_sha256": sha256(
                    inputs.frame_map_path.read_bytes()
                ).hexdigest(),
                "configuration_sha256": sha256(config_path.read_bytes()).hexdigest(),
                "metric_scale_locked": True,
                "position_source": "srt_cad_locked",
            }
            reconstruction, sparse_points = _fixed_track_reconstruction_paths(inputs)
            if reconstruction.is_file():
                outputs["reconstruction_trajectory"] = str(reconstruction)
                reconstruction_sha = sha256(reconstruction.read_bytes()).hexdigest()
                validation_proof["reconstruction_trajectory_sha256"] = (
                    reconstruction_sha
                )
                digest.update(reconstruction.read_bytes())
            if sparse_points is not None and sparse_points.is_file():
                outputs["reconstruction_sparse_points"] = str(sparse_points)
                sparse_sha = sha256(sparse_points.read_bytes()).hexdigest()
                validation_proof["reconstruction_sparse_points_sha256"] = sparse_sha
                digest.update(sparse_points.read_bytes())
        fingerprint = digest.hexdigest()
        return AdapterResult.success(
            output_revision=f"{self.name}:{fingerprint[:16]}",
            output_fingerprint=fingerprint,
            outputs=outputs,
            progress=(
                AdapterProgress(
                    stage="validated",
                    message=f"validated {self.name} trajectory output",
                    fraction=1.0,
                ),
            ),
            validation_proof=validation_proof,
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
        if self.name == "srt_fixed_track_visual_pose":
            settings = inputs.parameters.get("srt_fixed_track_visual_pose")
            metadata = inputs.parameters.get("video_metadata")
            if not isinstance(settings, Mapping) or not isinstance(metadata, Mapping):
                raise ValueError("fixed-track COLMAP requires SRT settings and video metadata")
            source_width = int(metadata.get("width", 0))
            source_height = int(metadata.get("height", 0))
            fov = float(settings.get("horizontal_fov_deg", 0.0))
            if (
                source_width <= 0
                or source_height <= 0
                or not 1.0 < fov < 179.0
            ):
                raise ValueError("fixed-track COLMAP requires valid video size and FOV")
            resolution = normalize_reconstruction_resolution(
                settings.get("reconstruction_resolution")
            )
            width, height = reconstruction_dimensions(
                source_width,
                source_height,
                resolution,
            )
            focal = width / (2.0 * math.tan(math.radians(fov) * 0.5))
            camera_params = ",".join(
                f"{value:.12g}"
                for value in (focal, width * 0.5, height * 0.5, 0.0, 0.0)
            )
            command.extend(
                [
                    "--backend",
                    "colmap_cli",
                    "--device",
                    str(inputs.parameters.get("device", "auto")),
                    "--max-image-size",
                    str(max(width, height)),
                    "--max-num-features",
                    "8000",
                    "--sequential-overlap",
                    "10",
                    "--ba-global-max-num-iterations",
                    "15",
                    "--camera-model",
                    "RADIAL",
                    "--camera-params",
                    camera_params,
                    "--source-frames-file",
                    str(inputs.attempt_directory / "adaptive_frame_plan.json"),
                    "--prepared-images-dir",
                    str(self._run_root(inputs) / "02_sfm" / "images"),
                ]
            )
            target_height = target_height_for_resolution(resolution)
            if target_height is not None and source_height > target_height:
                command.extend(["--reconstruction-height", str(target_height)])
        for key in (
            ("gpu_index",)
            if self.name == "srt_fixed_track_visual_pose"
            else ("backend", "device", "gpu_index")
        ):
            value = inputs.parameters.get(key)
            if value is not None:
                command.extend([f"--{key.replace('_', '-')}", str(value)])
        return tuple(command)

    def _fixed_track_plan_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        if inputs.srt_path is None or inputs.frame_map_path is None:
            raise FileNotFoundError("fixed-track SRT and frame map are required")
        settings = inputs.parameters.get("srt_fixed_track_visual_pose")
        metadata = inputs.parameters.get("video_metadata")
        if not isinstance(settings, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("fixed-track planner requires SRT settings and video metadata")
        width, height = reconstruction_dimensions(
            int(metadata.get("width", 0)),
            int(metadata.get("height", 0)),
            normalize_reconstruction_resolution(settings.get("reconstruction_resolution")),
        )
        command = [
            sys.executable,
            "-m",
            "cadscene.cli.plan_srt_adaptive_frames",
            "--video",
            str(inputs.video_path),
            "--srt",
            str(inputs.srt_path),
            "--frame-map",
            str(inputs.frame_map_path),
            "--config",
            str(_fixed_track_config_path(inputs)),
            "--output",
            str(inputs.attempt_directory / "adaptive_frame_plan.json"),
            "--images-output",
            str(self._run_root(inputs) / "02_sfm" / "images"),
            "--reconstruct-width",
            str(width),
            "--reconstruct-height",
            str(height),
            "--progress-file",
            str(inputs.attempt_directory / "adapter_progress.json"),
        ]
        cache_root = inputs.parameters.get("prepared_frame_cache_root")
        if cache_root not in (None, ""):
            command.extend(["--cache-root", str(cache_root)])
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

    def _sfm_partition_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        if inputs.frame_map_path is None or inputs.core_frame_map_path is None:
            raise FileNotFoundError("SfM solve/core frame maps are required")
        trajectory = self._run_root(inputs) / "02_sfm/camera_trajectory.json"
        return (
            sys.executable,
            "-m",
            "cadscene.cli.partition_sfm_trajectory",
            "--trajectory",
            str(trajectory),
            "--solve-frame-map",
            str(inputs.frame_map_path),
            "--core-frame-map",
            str(inputs.core_frame_map_path),
            "--solve-output",
            str(self._run_root(inputs) / "02_sfm/camera_trajectory_solve.json"),
            "--core-output",
            str(trajectory),
        )

    def _full_pose_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        if inputs.srt_path is None or inputs.frame_map_path is None:
            raise FileNotFoundError("full-pose SRT and frame map are required")
        return (
            sys.executable,
            "-m",
            "cadscene.cli.build_srt_full_pose",
            "--dataset",
            inputs.project_id,
            "--run-id",
            inputs.clip_id,
            "--output-root",
            str(inputs.attempt_directory),
            "--video",
            str(inputs.video_path),
            "--srt",
            str(inputs.srt_path),
            "--frame-map",
            str(inputs.frame_map_path),
            "--config",
            str(_full_pose_config_path(inputs)),
            "--progress-file",
            str(inputs.attempt_directory / "adapter_progress.json"),
        )

    def _fixed_track_command(self, inputs: AdapterInputs) -> tuple[str, ...]:
        if inputs.srt_path is None or inputs.frame_map_path is None:
            raise FileNotFoundError("fixed-track SRT and frame map are required")
        reconstruction, sparse_points = _fixed_track_reconstruction_paths(inputs)
        command = [
            sys.executable,
            "-m",
            "cadscene.cli.build_srt_fixed_track_visual_pose",
            "--dataset",
            inputs.project_id,
            "--run-id",
            inputs.clip_id,
            "--output-root",
            str(inputs.attempt_directory),
            "--video",
            str(inputs.video_path),
            "--srt",
            str(inputs.srt_path),
            "--frame-map",
            str(inputs.frame_map_path),
            "--config",
            str(_fixed_track_config_path(inputs)),
            "--reconstruction-trajectory",
            str(reconstruction),
            "--progress-file",
            str(inputs.attempt_directory / "adapter_progress.json"),
        ]
        if sparse_points is not None:
            command.extend(["--sparse-ply", str(sparse_points)])
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
            "--progress-file",
            str(inputs.attempt_directory / "adapter_progress.json"),
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
        if inputs.frame_map_path is not None:
            command.extend(["--frame-map", str(inputs.frame_map_path)])
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
                version="2",
                srt_requirement="none",
                modules=(
                    "cadscene.cli.run_sfm",
                    "cadscene.cli.partition_sfm_trajectory",
                ),
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
                version="6",
                srt_requirement="full_pose",
                modules=("cadscene.cli.build_srt_full_pose",),
                output_relative_path="02_srt_full_pose/camera_trajectory_full_pose.json",
            ),
            ExistingWorkflowAdapter(
                name="srt_fixed_track_visual_pose",
                version="8",
                srt_requirement="fixed_track",
                modules=(
                    "cadscene.cli.plan_srt_adaptive_frames",
                    "cadscene.cli.run_sfm",
                    "cadscene.cli.build_srt_fixed_track_visual_pose",
                ),
                output_relative_path=(
                    "02_srt_visual_pose/camera_trajectory_visual_pose.json"
                ),
            ),
            ExistingWorkflowAdapter(
                name="pure_rotation",
                version="2",
                srt_requirement="none",
                modules=("cadscene.cli.run_pure_rotation",),
                output_relative_path="02_pure_rotation/camera_rotation_raw.json",
                pure_rotation_backend_root=pure_rotation_backend_root,
                pure_rotation_backend_command=pure_rotation_backend_command,
                pure_rotation_calibration_root=pure_rotation_calibration_root,
            ),
        )
    )


def _has_reconstruction_reuse(inputs: AdapterInputs) -> bool:
    return isinstance(inputs.parameters.get("reconstruction_reuse"), Mapping)


def _fixed_track_reconstruction_paths(
    inputs: AdapterInputs,
) -> tuple[Path, Path | None]:
    reuse = inputs.parameters.get("reconstruction_reuse")
    if isinstance(reuse, Mapping):
        trajectory_value = reuse.get("trajectory_path")
        trajectory_sha256 = reuse.get("trajectory_sha256")
        sparse_value = reuse.get("sparse_points_path")
        sparse_sha256 = reuse.get("sparse_points_sha256")
        if (
            not isinstance(trajectory_value, str)
            or not trajectory_value
            or not isinstance(trajectory_sha256, str)
        ):
            raise ValueError(
                "reconstruction reuse requires trajectory_path and trajectory_sha256"
            )
        trajectory = Path(trajectory_value)
        if not trajectory.is_file():
            raise FileNotFoundError(
                f"reused COLMAP trajectory is missing: {trajectory}"
            )
        if sha256(trajectory.read_bytes()).hexdigest() != trajectory_sha256:
            raise ValueError("reused COLMAP trajectory fingerprint changed")
        sparse = (
            Path(sparse_value)
            if isinstance(sparse_value, str) and sparse_value
            else None
        )
        if sparse is not None:
            if not isinstance(sparse_sha256, str):
                raise ValueError("reused COLMAP sparse points require sparse_points_sha256")
            if not sparse.is_file():
                raise FileNotFoundError(
                    f"reused COLMAP sparse points are missing: {sparse}"
                )
            if sha256(sparse.read_bytes()).hexdigest() != sparse_sha256:
                raise ValueError("reused COLMAP sparse points fingerprint changed")
        return trajectory, sparse
    run_root = inputs.attempt_directory / inputs.project_id / inputs.clip_id
    return (
        run_root / "02_sfm/camera_trajectory.json",
        run_root / "02_sfm/sparse_points.ply",
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


def _full_pose_config_path(inputs: AdapterInputs) -> Path:
    return inputs.attempt_directory / "srt_full_pose_config.json"


def _full_pose_config_payload(inputs: AdapterInputs) -> dict[str, object]:
    if (
        inputs.source_start_pts is None
        or inputs.source_end_pts_exclusive is None
        or inputs.source_time_base is None
    ):
        raise ValueError("srt_full_pose requires an authoritative source interval")
    parameters = inputs.parameters
    georeference = parameters.get("cad_georeference")
    settings = parameters.get("srt_full_pose")
    video_metadata = parameters.get("video_metadata")
    origin = parameters.get("cad_origin_xy")
    if not isinstance(georeference, Mapping):
        raise ValueError("confirmed cad_georeference is required")
    if not isinstance(settings, Mapping):
        raise ValueError("srt_full_pose settings are required")
    if "horizontal_fov_deg" not in settings:
        raise ValueError("horizontal_fov_deg is required")
    if not isinstance(video_metadata, Mapping):
        raise ValueError("video_metadata is required")
    if not isinstance(origin, (list, tuple)) or len(origin) != 2:
        raise ValueError("cad_origin_xy must contain two values")
    build = FullPoseBuildConfig.from_dict(
        {
            "clip_id": inputs.clip_id,
            "source_start_pts": inputs.source_start_pts,
            "source_end_pts_exclusive": inputs.source_end_pts_exclusive,
            "source_time_base": {
                "numerator": inputs.source_time_base.numerator,
                "denominator": inputs.source_time_base.denominator,
            },
            "georeference": dict(georeference),
            "cad_origin_xy": list(origin),
            "cad_scale": parameters.get("cad_scale"),
            "horizontal_fov_deg": settings["horizontal_fov_deg"],
            "cad_z_offset_m": settings.get("cad_z_offset_m", 0.0),
            "attitude_profile": settings.get(
                "attitude_profile", "dji_absolute_ned"
            ),
            "max_interpolation_gap_sec": settings.get(
                "max_interpolation_gap_sec", 1.5
            ),
            "minimum_registered_coverage": settings.get(
                "minimum_registered_coverage", 0.8
            ),
            "max_horizontal_speed_mps": settings.get(
                "max_horizontal_speed_mps", 100.0
            ),
        }
    )
    return {
        "video_metadata": dict(video_metadata), "build": build.to_dict(),
        "terrain_source_paths": list(parameters.get("terrain_source_paths", ())),
        "terrain_source_fingerprints": list(parameters.get("terrain_source_fingerprints", ())),
        "cad_asset_fingerprint": parameters.get("cad_asset_fingerprint"),
    }


def _write_full_pose_config(inputs: AdapterInputs) -> Path:
    path = _full_pose_config_path(inputs)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            _full_pose_config_payload(inputs),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _fixed_track_config_path(inputs: AdapterInputs) -> Path:
    return inputs.attempt_directory / "srt_fixed_track_visual_pose_config.json"


def _fixed_track_config_payload(inputs: AdapterInputs) -> dict[str, object]:
    if (
        inputs.source_start_pts is None
        or inputs.source_end_pts_exclusive is None
        or inputs.source_time_base is None
    ):
        raise ValueError(
            "srt_fixed_track_visual_pose requires an authoritative source interval"
        )
    parameters = inputs.parameters
    georeference = parameters.get("cad_georeference")
    settings = parameters.get("srt_fixed_track_visual_pose")
    video_metadata = parameters.get("video_metadata")
    origin = parameters.get("cad_origin_xy")
    if not isinstance(georeference, Mapping):
        raise ValueError("confirmed cad_georeference is required")
    if not isinstance(settings, Mapping):
        raise ValueError("srt_fixed_track_visual_pose settings are required")
    if "horizontal_fov_deg" not in settings:
        raise ValueError("horizontal_fov_deg is required")
    if not isinstance(video_metadata, Mapping):
        raise ValueError("video_metadata is required")
    if not isinstance(origin, (list, tuple)) or len(origin) != 2:
        raise ValueError("cad_origin_xy must contain two values")
    build = FixedTrackVisualPoseConfig.from_dict(
        {
            "clip_id": inputs.clip_id,
            "source_start_pts": inputs.source_start_pts,
            "source_end_pts_exclusive": inputs.source_end_pts_exclusive,
            "source_time_base": {
                "numerator": inputs.source_time_base.numerator,
                "denominator": inputs.source_time_base.denominator,
            },
            "georeference": dict(georeference),
            "cad_origin_xy": list(origin),
            "cad_scale": parameters.get("cad_scale"),
            "horizontal_fov_deg": settings["horizontal_fov_deg"],
            "reconstruction_resolution": normalize_reconstruction_resolution(
                settings.get("reconstruction_resolution")
            ),
            "route_offset_xyz_m": settings.get(
                "route_offset_xyz_m", (0.0, 0.0, 0.0)
            ),
            "max_interpolation_gap_sec": settings.get(
                "max_interpolation_gap_sec", 1.5
            ),
            "minimum_position_coverage": settings.get(
                "minimum_position_coverage", 0.8
            ),
            "keyframe_interval_sec": settings.get("keyframe_interval_sec", 0.5),
            "max_features": settings.get("max_features", 2000),
            "min_pair_matches": settings.get("min_pair_matches", 24),
            "max_orientation_interpolation_gap_sec": settings.get(
                "max_orientation_interpolation_gap_sec", 2.0
            ),
            "max_horizontal_speed_mps": settings.get(
                "max_horizontal_speed_mps", 100.0
            ),
        }
    )
    return {
        "video_metadata": dict(video_metadata),
        "build": build.to_dict(),
        "terrain_source_paths": list(parameters.get("terrain_source_paths", ())),
        "terrain_source_fingerprints": list(
            parameters.get("terrain_source_fingerprints", ())
        ),
        "cad_asset_fingerprint": parameters.get("cad_asset_fingerprint"),
    }


def _write_fixed_track_config(inputs: AdapterInputs) -> Path:
    path = _fixed_track_config_path(inputs)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            _fixed_track_config_payload(inputs),
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
