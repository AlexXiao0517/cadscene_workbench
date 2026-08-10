from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import sys

from .adapters import AdapterProgress, AdapterResult
from .render_adapters import (
    RenderAdapterRegistry,
    RenderExecutionPlan,
    RenderInputs,
)


@dataclass(frozen=True)
class ExistingWorkbenchRenderAdapter:
    """Run the existing renderer against immutable project workbench inputs."""

    workflow: str
    application_root: Path
    version: str = "1"

    @property
    def name(self) -> str:
        return f"existing-{self.workflow}-render"

    def prepare(self, inputs: RenderInputs) -> RenderExecutionPlan:
        if inputs.workflow != self.workflow:
            raise ValueError("render workflow does not match adapter")
        cad_path = _required_path(inputs.parameters, "cad_dataset_path")
        cad_scale = _positive_number(inputs.parameters.get("cad_scale"), "cad_scale")
        origin_xy = _origin_xy(inputs.parameters.get("origin_xy"))
        inputs.attempt_directory.mkdir(parents=True, exist_ok=True)
        media_spec_path = inputs.attempt_directory / "project_media_spec.json"
        media_spec_path.write_text(
            json.dumps(
                inputs.project_media_spec.to_dict(), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        output_root = inputs.attempt_directory
        legacy_output = (
            output_root
            / inputs.project_id
            / inputs.clip_id
            / "08_render"
            / "sfm_align_overlay.mp4"
        )
        if self.workflow == "pure_rotation":
            render_command = (
                sys.executable,
                "-m",
                "cadscene.cli.render_pure_rotation",
                "--dataset",
                inputs.project_id,
                "--run-id",
                inputs.clip_id,
                "--output-root",
                str(output_root),
                "--video",
                str(inputs.physical_video_path),
                "--cad-dir",
                str(cad_path),
                "--cad-scale",
                str(cad_scale),
                "--origin-xy",
                str(origin_xy[0]),
                str(origin_xy[1]),
                "--track",
                str(inputs.workbench_artifact_path),
                "--faded-overlay",
                "--fade-start-m",
                "250",
                "--max-distance-m",
                "900",
            )
        else:
            trajectory = _required_path(inputs.parameters, "trajectory_path")
            run_root = trajectory.parent.parent
            sparse_ply = run_root / "02_sfm" / "sparse_points.ply"
            if not sparse_ply.is_file():
                raise FileNotFoundError(f"SfM sparse point cloud is unavailable: {sparse_ply}")
            render_command = (
                sys.executable,
                "-m",
                "cadscene.cli.run_pipeline",
                "--dataset",
                inputs.project_id,
                "--run-id",
                inputs.clip_id,
                "--output-root",
                str(output_root),
                "--config",
                str(
                    self.application_root
                    / "configs"
                    / "pipelines"
                    / "sfm_overlay_existing_sfm.yaml"
                ),
                "--stages",
                "alignment,render",
                "--trajectory",
                str(trajectory),
                "--sparse-ply",
                str(sparse_ply),
                "--web-camera-track",
                str(inputs.workbench_artifact_path),
                "--video",
                str(inputs.physical_video_path),
                "--cad-dir",
                str(cad_path),
                "--cad-scale",
                str(cad_scale),
                "--origin-xy",
                str(origin_xy[0]),
                str(origin_xy[1]),
            )
        package_command = (
            sys.executable,
            "-m",
            "cadscene.cli.package_project_render",
            "--input",
            str(legacy_output),
            "--source-frame-map",
            str(inputs.authoritative_frame_map_path),
            "--media-spec",
            str(media_spec_path),
            "--output-dir",
            str(inputs.attempt_directory),
        )
        return RenderExecutionPlan(
            commands=(render_command, package_command),
            validate=lambda: _validate_packaged_render(inputs),
        )


def default_workbench_render_adapters(
    *, application_root: Path
) -> RenderAdapterRegistry:
    root = Path(application_root).resolve()
    return RenderAdapterRegistry(
        tuple(
            ExistingWorkbenchRenderAdapter(workflow=workflow, application_root=root)
            for workflow in (
                "sfm_only",
                "srt_sfm_fused",
                "srt_full_pose",
                "pure_rotation",
            )
        )
    )


def _validate_packaged_render(inputs: RenderInputs) -> AdapterResult:
    video = inputs.attempt_directory / "rendered.mp4"
    frame_map = inputs.attempt_directory / "render_frame_map.json"
    if not video.is_file() or not frame_map.is_file():
        return AdapterResult.failed("packaged render outputs are missing")
    fingerprint = sha256(video.read_bytes() + frame_map.read_bytes()).hexdigest()
    return AdapterResult.success(
        output_revision=f"render-{fingerprint[:16]}",
        output_fingerprint=fingerprint,
        outputs={"video": str(video), "frame_map": str(frame_map)},
        progress=(
            AdapterProgress(
                stage="packaged", message="render output packaged", fraction=1.0
            ),
        ),
    )


def _required_path(parameters, key: str) -> Path:
    value = parameters.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    path = Path(value).resolve(strict=True)
    if not path.exists():
        raise FileNotFoundError(f"{key} is unavailable: {path}")
    return path


def _positive_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be positive") from exc
    if result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _origin_xy(value: object) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("origin_xy must contain two values")
    return float(value[0]), float(value[1])
