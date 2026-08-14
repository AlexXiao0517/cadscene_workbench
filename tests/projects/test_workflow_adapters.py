from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from cadscene.projects.adapters import AdapterInputs
from cadscene.projects.workflow_adapters import default_workflow_adapters
import cadscene.projects.workflow_adapters as workflow_adapters_module


WORKFLOWS = ("sfm_only", "srt_sfm_fused", "srt_full_pose", "pure_rotation")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_resolved_workflow_selects_existing_adapter(workflow: str) -> None:
    registry = default_workflow_adapters()

    assert registry.for_workflow(workflow).name == workflow


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_adapters_declare_physical_mp4_and_srt_requirements(workflow: str) -> None:
    adapter = default_workflow_adapters().for_workflow(workflow)

    assert adapter.requires_physical_mp4 is True
    assert adapter.srt_requirement == {
        "sfm_only": "none",
        "srt_sfm_fused": "trajectory",
        "srt_full_pose": "full_pose",
        "pure_rotation": "none",
    }[workflow]


def test_sfm_adapter_wraps_existing_cli_and_validates_attempt_output(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    attempt = tmp_path / "attempt-1"
    adapter = default_workflow_adapters().for_workflow("sfm_only")
    inputs = AdapterInputs(
        project_id="p1",
        clip_id="c1",
        video_path=video,
        srt_path=None,
        attempt_directory=attempt,
    )

    command = adapter.build_command(adapter.prepare_inputs(inputs))

    assert command[1:3] == ("-m", "cadscene.cli.run_sfm")
    assert str(video) in command
    assert command[command.index("--progress-file") + 1] == str(
        attempt / "adapter_progress.json"
    )
    output = attempt / "02_sfm/camera_trajectory.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"poses": [{}]}), encoding="utf-8")
    result = adapter.validate_outputs(inputs)
    assert result.status == "success"
    assert result.output_revision is not None
    assert result.output_fingerprint is not None
    assert result.outputs == {"trajectory": str(output)}
    assert result.progress[0].fraction == 1.0


def test_sfm_adapter_uses_existing_sfm_interpreter_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_python = tmp_path / "difusser/python.exe"
    monkeypatch.setattr(
        workflow_adapters_module, "resolve_sfm_python", lambda: resolved_python
    )
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    inputs = AdapterInputs("p1", "c1", video, None, tmp_path / "attempt")

    command = default_workflow_adapters().for_workflow("sfm_only").build_command(
        inputs
    )

    assert command[0] == str(resolved_python)


def test_partial_srt_adapter_wraps_sfm_then_existing_fusion_cli(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "clip.srt"
    video.write_bytes(b"mp4")
    srt.write_text("1", encoding="utf-8")
    frame_map = tmp_path / "clip_frame_map.json"
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 25},
                "clips": [
                    {
                        "clip_id": "c1",
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 100,
                        "frames": [{"ordinal": 0, "pts": 0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    inputs = AdapterInputs(
        "p1",
        "c1",
        video,
        srt,
        tmp_path / "attempt-1",
        source_start_pts=0,
        source_end_pts_exclusive=100,
        source_time_base=Fraction(1, 25),
        frame_map_path=frame_map,
    )
    adapter = default_workflow_adapters().for_workflow("srt_sfm_fused")

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))

    assert [command[2] for command in commands] == [
        "cadscene.cli.run_sfm",
        "cadscene.cli.fuse_srt_sfm",
    ]
    output = (
        inputs.attempt_directory
        / inputs.project_id
        / inputs.clip_id
        / "02_fusion/camera_trajectory_fused.json"
    )
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"poses": [{}]}), encoding="utf-8")

    assert adapter.validate_outputs(inputs).status == "success"


def test_partial_srt_nonzero_clip_uses_exact_pts_derived_source_offset(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "source.srt"
    video.write_bytes(b"mp4")
    srt.write_text("1", encoding="utf-8")
    frame_map = tmp_path / "clip_frame_map.json"
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1001, "denominator": 30000},
                "clips": [
                    {
                        "clip_id": "c1",
                        "source_start_pts": 150,
                        "source_end_pts_exclusive": 450,
                        "frames": [
                            {"ordinal": 150, "pts": 150},
                            {"ordinal": 449, "pts": 449},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    inputs = AdapterInputs(
        "p1",
        "c1",
        video,
        srt,
        tmp_path / "attempt",
        source_start_pts=150,
        source_end_pts_exclusive=450,
        source_time_base=Fraction(1001, 30000),
        frame_map_path=frame_map,
    )
    adapter = default_workflow_adapters().for_workflow("srt_sfm_fused")

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))
    fusion = commands[1]

    assert fusion[fusion.index("--time-offset-sec") + 1] == "5.005"


def test_partial_srt_rejects_frame_map_interval_mismatch(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "source.srt"
    video.write_bytes(b"mp4")
    srt.write_text("1", encoding="utf-8")
    frame_map = tmp_path / "clip_frame_map.json"
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 25},
                "clips": [
                    {
                        "clip_id": "c1",
                        "source_start_pts": 1,
                        "source_end_pts_exclusive": 100,
                        "frames": [{"ordinal": 0, "pts": 1}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    inputs = AdapterInputs(
        "p1",
        "c1",
        video,
        srt,
        tmp_path / "attempt",
        source_start_pts=0,
        source_end_pts_exclusive=100,
        source_time_base=Fraction(1, 25),
        frame_map_path=frame_map,
    )

    with pytest.raises(ValueError, match="frame map.*interval"):
        default_workflow_adapters().for_workflow("srt_sfm_fused").prepare_inputs(
            inputs
        )


def test_full_pose_adapter_preserves_interface_only_existing_contract(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "clip.srt"
    video.write_bytes(b"mp4")
    srt.write_text("full pose", encoding="utf-8")
    inputs = AdapterInputs("p1", "c1", video, srt, tmp_path / "attempt-1")
    adapter = default_workflow_adapters().for_workflow("srt_full_pose")

    assert adapter.available is False
    assert adapter.unavailable_reason is not None
    assert "interface-only" in adapter.unavailable_reason
    with pytest.raises(NotImplementedError, match="interface-only"):
        adapter.build_commands(adapter.prepare_inputs(inputs))


def test_pure_rotation_adapter_wraps_existing_cli(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    inputs = AdapterInputs("p1", "c1", video, None, tmp_path / "attempt-1")
    adapter = default_workflow_adapters().for_workflow("pure_rotation")

    command = adapter.build_command(adapter.prepare_inputs(inputs))

    assert command[1:3] == ("-m", "cadscene.cli.run_pure_rotation")


def test_pure_rotation_adapter_passes_configured_external_backend(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    backend = tmp_path / "pure-rotation-backend"
    backend.mkdir()
    command_prefix = ("C:/conda/python.exe", "C:/backend/run.py")
    inputs = AdapterInputs("p1", "c1", video, None, tmp_path / "attempt-1")
    adapter = default_workflow_adapters(
        pure_rotation_backend_root=backend,
        pure_rotation_backend_command=command_prefix,
    ).for_workflow("pure_rotation")

    command = adapter.build_command(adapter.prepare_inputs(inputs))

    assert command[command.index("--backend-root") + 1] == str(backend)
    encoded = command[command.index("--backend-command-json") + 1]
    assert json.loads(encoded) == list(command_prefix)


def test_pure_rotation_adapter_prepares_scaled_calibration_before_opengv(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    backend = tmp_path / "pure-rotation-backend"
    calibration_root = tmp_path / "calibration-source"
    backend.mkdir()
    calibration_root.mkdir()
    command_prefix = ("C:/conda/python.exe", "C:/backend/run.py")
    inputs = AdapterInputs("p1", "c1", video, None, tmp_path / "attempt-1")
    adapter = default_workflow_adapters(
        pure_rotation_backend_root=backend,
        pure_rotation_backend_command=command_prefix,
        pure_rotation_calibration_root=calibration_root,
    ).for_workflow("pure_rotation")

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))

    assert len(commands) == 2
    assert commands[0][1:3] == (
        "-m",
        "cadscene.cli.prepare_pure_rotation_calibration",
    )
    assert commands[0][commands[0].index("--source-root") + 1] == str(
        calibration_root
    )
    assert commands[1][commands[1].index("--cadscene-readonly") + 1] == str(
        inputs.attempt_directory / "pure_rotation_calibration"
    )


def test_preflight_rejects_missing_physical_inputs(tmp_path: Path) -> None:
    adapter = default_workflow_adapters().for_workflow("srt_sfm_fused")
    inputs = AdapterInputs(
        "p1", "c1", tmp_path / "missing.mp4", tmp_path / "missing.srt", tmp_path / "attempt"
    )

    with pytest.raises(FileNotFoundError, match="physical MP4"):
        adapter.prepare_inputs(inputs)


def test_adapter_validation_does_not_modify_manifests(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    manifest = tmp_path / "jobs_manifest.json"
    manifest.write_text("unchanged", encoding="utf-8")
    inputs = AdapterInputs("p1", "c1", video, None, tmp_path / "attempt")
    adapter = default_workflow_adapters().for_workflow("sfm_only")

    adapter.validate_outputs(inputs)

    assert manifest.read_text(encoding="utf-8") == "unchanged"
