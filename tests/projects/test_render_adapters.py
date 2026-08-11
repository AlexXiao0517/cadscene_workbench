from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cadscene.projects.adapters import AdapterProgress, AdapterResult
from cadscene.projects.media import ProjectMediaSpec, probe_media
from cadscene.projects.render_adapters import (
    RenderAdapterRegistry,
    RenderExecutionPlan,
    RenderInputs,
)
from cadscene.projects.workbench_render_adapter import default_workbench_render_adapters
from cadscene.cli.package_project_render import (
    _normalization_command,
    _render_frame_map,
    main as package_project_render_main,
)
from cadscene.video_analysis.pts import DecodedFrameTimestamp
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


def _media_spec() -> ProjectMediaSpec:
    return ProjectMediaSpec(
        width=1920,
        height=1080,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 1000),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=Fraction(25, 1),
    )


def _render_inputs(tmp_path: Path) -> RenderInputs:
    video = (tmp_path / "clip.mp4").resolve()
    video.write_bytes(b"video")
    frame_map = (tmp_path / "clip_frame_map.json").resolve()
    frame_map.write_text("{}", encoding="utf-8")
    workbench = (tmp_path / "workbench_output_manifest.json").resolve()
    workbench.write_text("{}", encoding="utf-8")
    return RenderInputs(
        project_id="project-1",
        clip_id="clip-1",
        workflow="sfm_only",
        physical_video_path=video,
        authoritative_frame_map_path=frame_map,
        authoritative_source_frames=(
            DecodedFrameTimestamp(0, 5000, 40, "pts"),
            DecodedFrameTimestamp(1, 5040, 40, "best_effort_timestamp"),
        ),
        source_time_base=Fraction(1, 1000),
        workbench_artifact_path=workbench,
        workbench_output_revision="workbench-output-1",
        workbench_output_fingerprint="a" * 64,
        attempt_directory=(tmp_path / "attempt-1").resolve(),
        project_media_spec=_media_spec(),
        parameters={"quality": "preview"},
    )


class _FakeRenderAdapter:
    workflow = "sfm_only"
    name = "fake-render"
    version = "1"

    def __init__(self) -> None:
        self.received: RenderInputs | None = None

    def prepare(self, inputs: RenderInputs) -> RenderExecutionPlan:
        self.received = inputs
        return RenderExecutionPlan(
            commands=(("fake-render", str(inputs.physical_video_path)),),
            validate=lambda: AdapterResult.success(
                output_revision="render-output-1",
                output_fingerprint="b" * 64,
                outputs={"video": str(inputs.attempt_directory / "rendered.mp4")},
                progress=(
                    AdapterProgress(
                        stage="validating", message="validating output"
                    ),
                ),
            ),
        )


def test_fake_adapter_receives_only_validated_inputs_and_returns_structured_plan(
    tmp_path: Path,
) -> None:
    inputs = _render_inputs(tmp_path)
    manifest = tmp_path / "render_manifest.json"
    manifest.write_text('{"revision": 7}', encoding="utf-8")
    before = manifest.read_bytes()
    adapter = _FakeRenderAdapter()

    plan = adapter.prepare(inputs)
    result = plan.validate()

    assert adapter.received == inputs
    assert not hasattr(inputs, "repositories")
    assert plan.commands[0][0] == "fake-render"
    assert result.status == "success"
    assert result.progress[0].to_dict() == {
        "stage": "validating",
        "message": "validating output",
    }
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_id", "", "project_id"),
        ("clip_id", "../escape", "clip_id"),
        ("workflow", "", "workflow"),
        ("physical_video_path", Path("relative.mp4"), "absolute"),
        ("authoritative_frame_map_path", Path("relative.json"), "absolute"),
        ("workbench_artifact_path", Path("relative.json"), "absolute"),
        ("attempt_directory", Path("relative-attempt"), "absolute"),
        ("authoritative_source_frames", (), "source frames"),
        ("source_time_base", 0.001, "positive Fraction"),
        ("workbench_output_revision", "", "workbench output revision"),
        ("workbench_output_fingerprint", "bad", "fingerprint"),
        ("project_media_spec", object(), "ProjectMediaSpec"),
    ],
)
def test_render_inputs_fail_closed_on_invalid_identity_path_or_authority(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    valid = _render_inputs(tmp_path)
    payload = dict(valid.__dict__)
    payload[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        RenderInputs(**payload)


def test_render_inputs_require_existing_immutable_inputs(tmp_path: Path) -> None:
    valid = _render_inputs(tmp_path)
    missing = (tmp_path / "missing.mp4").resolve()

    with pytest.raises(ValueError, match="physical video"):
        RenderInputs(**{**valid.__dict__, "physical_video_path": missing})


def test_render_inputs_recursively_snapshot_json_parameters(tmp_path: Path) -> None:
    nested = {"quality": {"levels": [1, 2]}}
    valid = _render_inputs(tmp_path)

    inputs = RenderInputs(**{**valid.__dict__, "parameters": nested})
    nested["quality"]["levels"].append(3)

    assert tuple(inputs.parameters["quality"]["levels"]) == (1, 2)
    with pytest.raises(TypeError):
        inputs.parameters["quality"]["changed"] = True


@pytest.mark.parametrize("invalid", [object(), float("nan"), float("inf")])
def test_render_inputs_reject_non_json_or_nonfinite_parameters(
    tmp_path: Path, invalid: object,
) -> None:
    valid = _render_inputs(tmp_path)

    with pytest.raises((TypeError, ValueError), match="parameters"):
        RenderInputs(**{**valid.__dict__, "parameters": {"invalid": invalid}})


@pytest.mark.parametrize(
    "field,value", [("ordinal", 0.0), ("ordinal", True), ("pts", 5000.0), ("pts", True)]
)
def test_render_inputs_require_integer_authoritative_frame_identity(
    tmp_path: Path, field: str, value: object,
) -> None:
    valid = _render_inputs(tmp_path)
    values = {
        "ordinal": 0,
        "pts": 5000,
        "duration_pts": 40,
        "timestamp_source": "pts",
    }
    values[field] = value
    frames = (
        DecodedFrameTimestamp(**values),
        DecodedFrameTimestamp(1, 5040, 40, "pts"),
    )

    with pytest.raises(ValueError, match="integer identity"):
        RenderInputs(**{**valid.__dict__, "authoritative_source_frames": frames})


def test_registry_routes_by_workflow_and_unknown_workflow_fails_closed() -> None:
    adapter = _FakeRenderAdapter()
    registry = RenderAdapterRegistry((adapter,))

    assert registry.for_workflow("sfm_only") is adapter
    with pytest.raises(KeyError, match="unsupported render workflow"):
        registry.for_workflow("pure_rotation")


def test_registry_rejects_duplicate_workflow_or_adapter_identity() -> None:
    first = _FakeRenderAdapter()
    duplicate_workflow = _FakeRenderAdapter()

    with pytest.raises(ValueError, match="workflow"):
        RenderAdapterRegistry((first, duplicate_workflow))

    class SameIdentityDifferentWorkflow(_FakeRenderAdapter):
        workflow = "pure_rotation"

    with pytest.raises(ValueError, match="name/version"):
        RenderAdapterRegistry((first, SameIdentityDifferentWorkflow()))


@pytest.mark.parametrize(
    "commands",
    [(), ((),), (("",),), (("render", ""),)],
)
def test_execution_plan_rejects_empty_commands_or_tokens(commands: object) -> None:
    with pytest.raises(ValueError, match="commands"):
        RenderExecutionPlan(commands=commands, validate=lambda: AdapterResult.failed("x"))


def test_execution_plan_requires_structured_adapter_result() -> None:
    plan = RenderExecutionPlan(
        commands=(("fake-render", "clip.mp4"),),
        validate=lambda: {"status": "success"},
    )

    with pytest.raises(TypeError, match="AdapterResult"):
        plan.validate()


@pytest.mark.parametrize(
    "workflow", ("sfm_only", "srt_sfm_fused", "srt_full_pose", "pure_rotation")
)
def test_default_workbench_render_adapters_cover_every_project_workflow(
    tmp_path: Path, workflow: str,
) -> None:
    registry = default_workbench_render_adapters(application_root=tmp_path)

    assert registry.for_workflow(workflow).workflow == workflow


def test_pure_rotation_render_uses_immutable_workbench_track_and_attempt_output(
    tmp_path: Path,
) -> None:
    inputs = _render_inputs(tmp_path)
    cad = (tmp_path / "cad").resolve()
    cad.mkdir()
    inputs = RenderInputs(
        **{
            **inputs.__dict__,
            "workflow": "pure_rotation",
            "parameters": {
                "cad_dataset_path": str(cad),
                "cad_scale": 0.06,
                "origin_xy": [0.0, 0.0],
            },
        }
    )
    adapter = default_workbench_render_adapters(
        application_root=tmp_path
    ).for_workflow("pure_rotation")

    plan = adapter.prepare(inputs)
    render_command, package_command = plan.commands

    assert "cadscene.cli.render_pure_rotation" in render_command
    assert str(inputs.workbench_artifact_path) in render_command
    assert str(inputs.attempt_directory) in render_command
    assert "--faded-overlay" not in render_command
    assert "--fade-start-m" not in render_command
    assert "cadscene.cli.package_project_render" in package_command
    assert str(inputs.authoritative_frame_map_path) in package_command


def test_render_packaging_preserves_authoritative_source_frame_identity(
    tmp_path: Path,
) -> None:
    frame_map = _render_frame_map(
        {
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "clips": [
                {
                    "frames": [
                        {"ordinal": 7, "pts": 5000},
                        {"ordinal": 8, "pts": 5040},
                    ]
                }
            ],
        }
    )
    command = _normalization_command(
        ffmpeg="ffmpeg",
        source=(tmp_path / "legacy.mp4").resolve(),
        target=(tmp_path / "rendered.mp4").resolve(),
        spec=_media_spec(),
    )

    assert frame_map["frames"] == [
        {
            "output_frame_ordinal": 0,
            "source_decoded_frame_ordinal": 7,
            "source_pts": 5000,
        },
        {
            "output_frame_ordinal": 1,
            "source_decoded_frame_ordinal": 8,
            "source_pts": 5040,
        },
    ]
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[command.index("-enc_time_base") + 1] == "1:1000"
    assert "-r" not in command


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for render packaging",
)
def test_render_packaging_cli_emits_validated_frame_count(tmp_path: Path) -> None:
    try:
        ffmpeg = str(resolve_ffmpeg_executable())
    except (FileNotFoundError, RuntimeError):
        pytest.skip("an H.264-capable FFmpeg is unavailable")
    source = tmp_path / "legacy.mp4"
    subprocess.run(
        (
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25:duration=0.08",
            "-frames:v", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(source),
        ),
        check=True,
    )
    source_map = tmp_path / "source_map.json"
    source_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "clips": [
                    {"frames": [{"ordinal": 7, "pts": 5000}, {"ordinal": 8, "pts": 5040}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    media_spec = tmp_path / "media_spec.json"
    media_spec.write_text(json.dumps(_media_spec().to_dict()), encoding="utf-8")

    result = package_project_render_main(
        [
            "--input", str(source),
            "--source-frame-map", str(source_map),
            "--media-spec", str(media_spec),
            "--output-dir", str(tmp_path),
        ]
    )

    assert result == 0
    assert probe_media(tmp_path / "rendered.mp4").video.frame_count == 2
    assert len(json.loads((tmp_path / "render_frame_map.json").read_text())["frames"]) == 2
