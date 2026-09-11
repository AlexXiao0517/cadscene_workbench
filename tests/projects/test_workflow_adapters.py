from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from cadscene.projects.adapters import AdapterInputs
from cadscene.projects.workflow_adapters import default_workflow_adapters
import cadscene.projects.workflow_adapters as workflow_adapters_module


WORKFLOWS = (
    "sfm_only",
    "srt_sfm_fused",
    "srt_full_pose",
    "srt_fixed_track_visual_pose",
    "pure_rotation",
)


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
        "srt_fixed_track_visual_pose": "fixed_track",
        "pure_rotation": "none",
    }[workflow]


def test_sfm_adapter_wraps_existing_cli_and_validates_attempt_output(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    solve_map = tmp_path / "solve-map.json"
    core_map = tmp_path / "core-map.json"
    solve_map.write_text("{}", encoding="utf-8")
    core_map.write_text("{}", encoding="utf-8")
    attempt = tmp_path / "attempt-1"
    adapter = default_workflow_adapters().for_workflow("sfm_only")
    inputs = AdapterInputs(
        project_id="p1",
        clip_id="c1",
        video_path=video,
        srt_path=None,
        attempt_directory=attempt,
        frame_map_path=solve_map,
        core_frame_map_path=core_map,
    )

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))
    command = commands[0]

    assert command[1:3] == ("-m", "cadscene.cli.run_sfm")
    assert commands[1][1:3] == ("-m", "cadscene.cli.partition_sfm_trajectory")
    assert str(video) in command
    assert command[command.index("--progress-file") + 1] == str(
        attempt / "adapter_progress.json"
    )
    output = attempt / "02_sfm/camera_trajectory.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"poses": [{}]}), encoding="utf-8")
    solve_output = output.with_name("camera_trajectory_solve.json")
    solve_output.write_text(json.dumps({"poses": [{}]}), encoding="utf-8")
    result = adapter.validate_outputs(inputs)
    assert result.status == "success"
    assert result.output_revision is not None
    assert result.output_fingerprint is not None
    assert result.outputs == {
        "trajectory": str(output),
        "solve_trajectory": str(solve_output),
        "solve_video": str(video),
        "solve_frame_map": str(solve_map),
        "core_frame_map": str(core_map),
    }
    assert result.progress[0].fraction == 1.0


def test_sfm_adapter_runs_one_sfm_then_partitions_trajectory(
    tmp_path: Path,
) -> None:
    video = tmp_path / "solve.mp4"
    video.write_bytes(b"mp4")
    solve_map = tmp_path / "solve-map.json"
    core_map = tmp_path / "core-map.json"
    solve_map.write_text("{}", encoding="utf-8")
    core_map.write_text("{}", encoding="utf-8")
    inputs = AdapterInputs(
        project_id="p1",
        clip_id="c1",
        video_path=video,
        srt_path=None,
        attempt_directory=tmp_path / "attempt",
        frame_map_path=solve_map,
        core_frame_map_path=core_map,
    )
    adapter = default_workflow_adapters().for_workflow("sfm_only")

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))
    modules = [
        command[index + 1]
        for command in commands
        for index, token in enumerate(command[:-1])
        if token == "-m"
    ]

    assert modules.count("cadscene.cli.run_sfm") == 1
    assert modules.count("cadscene.cli.partition_sfm_trajectory") == 1
    assert adapter.version == "2"


def test_pure_rotation_adapter_forwards_structured_progress_sidecar(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    attempt = tmp_path / "attempt-1"
    inputs = AdapterInputs("p1", "c1", video, None, attempt)

    command = default_workflow_adapters().for_workflow(
        "pure_rotation"
    ).build_command(inputs)

    assert command[command.index("--progress-file") + 1] == str(
        attempt / "adapter_progress.json"
    )


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
    solve_map = tmp_path / "solve-map.json"
    core_map = tmp_path / "core-map.json"
    solve_map.write_text("{}", encoding="utf-8")
    core_map.write_text("{}", encoding="utf-8")
    inputs = AdapterInputs(
        "p1",
        "c1",
        video,
        None,
        tmp_path / "attempt",
        frame_map_path=solve_map,
        core_frame_map_path=core_map,
    )

    command = default_workflow_adapters().for_workflow("sfm_only").build_commands(
        inputs
    )[0]

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


def _full_pose_georeference() -> dict[str, object]:
    return {
        "schema_version": 1,
        "horizontal_datum": "CGCS2000",
        "projection_family": "gauss_kruger",
        "zone_width_deg": 3,
        "central_meridian_deg": 120.0,
        "epsg": 4549,
        "projected_axis_order": "easting_northing",
        "cad_axis_mapping": "cad_x_easting_cad_y_northing",
        "zone_prefix": False,
        "linear_unit": "metre",
        "source": "user_confirmed",
        "confirmed": True,
        "confidence": 0.99,
        "validation": {"trajectory_inside_cad_ratio": 1.0},
    }


def _full_pose_parameters() -> dict[str, object]:
    return {
        "cad_georeference": _full_pose_georeference(),
        "srt_full_pose": {
            "horizontal_fov_deg": 82.0,
            "cad_z_offset_m": 100.0,
            "attitude_profile": "dji_absolute_ned",
        },
        "cad_origin_xy": [500_000.0, 3_320_113.3978450196],
        "cad_scale": 1.0,
        "video_metadata": {"width": 3840, "height": 2160, "fps": 25.0},
    }


def _full_pose_inputs(tmp_path: Path, *, include_fov: bool = True) -> AdapterInputs:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "clip.srt"
    frame_map = tmp_path / "clip_frame_map.json"
    video.write_bytes(b"mp4")
    srt.write_text("full pose", encoding="utf-8")
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
    parameters = _full_pose_parameters()
    if not include_fov:
        parameters["srt_full_pose"] = {
            "cad_z_offset_m": 100.0,
            "attitude_profile": "dji_absolute_ned",
        }
    return AdapterInputs(
        "p1",
        "c1",
        video,
        srt,
        tmp_path / "attempt-1",
        parameters=parameters,
        source_start_pts=0,
        source_end_pts_exclusive=100,
        source_time_base=Fraction(1, 25),
        frame_map_path=frame_map,
    )


def test_full_pose_adapter_is_available_and_runs_no_sfm(
    tmp_path: Path,
) -> None:
    inputs = _full_pose_inputs(tmp_path)
    adapter = default_workflow_adapters().for_workflow("srt_full_pose")

    prepared = adapter.prepare_inputs(inputs)
    commands = adapter.build_commands(prepared)

    assert adapter.available is True
    assert adapter.unavailable_reason is None
    assert adapter.version == "2"
    assert len(commands) == 1
    command = commands[0]
    assert command[1:3] == ("-m", "cadscene.cli.build_srt_full_pose")
    assert "cadscene.cli.run_sfm" not in command
    config = Path(command[command.index("--config") + 1])
    assert config.is_file()
    assert json.loads(config.read_text(encoding="utf-8"))["build"][
        "horizontal_fov_deg"
    ] == 82.0


def test_full_pose_prepare_requires_horizontal_fov(tmp_path: Path) -> None:
    inputs = _full_pose_inputs(tmp_path, include_fov=False)

    with pytest.raises(ValueError, match="horizontal_fov_deg"):
        default_workflow_adapters().for_workflow("srt_full_pose").prepare_inputs(
            inputs
        )


def test_full_pose_adapter_validates_trajectory_and_diagnostics(
    tmp_path: Path,
) -> None:
    inputs = _full_pose_inputs(tmp_path)
    adapter = default_workflow_adapters().for_workflow("srt_full_pose")
    prepared = adapter.prepare_inputs(inputs)
    output = (
        inputs.attempt_directory
        / "02_srt_full_pose"
        / "camera_trajectory_full_pose.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "poses": [{"registered": True}],
                "meta": {
                    "trajectory_mode": "srt_full_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                },
            }
        ),
        encoding="utf-8",
    )
    diagnostics = output.with_name("georeference_diagnostics.json")
    diagnostics.write_text(
        json.dumps({"registered_coverage": 1.0}), encoding="utf-8"
    )
    camera_path = output.with_name("camera_path_full_pose.csv")
    camera_path.write_text("frame_index,camera_x\n0,0\n", encoding="utf-8")
    report = output.with_name("full_pose_report.md")
    report.write_text("# report\n", encoding="utf-8")
    initial_track = output.parent.parent / "03_alignment/camera_track_pred.json"
    initial_track.parent.mkdir(parents=True, exist_ok=True)
    initial_track.write_text(
        json.dumps({"fps": 25.0, "keyframes": [{"frame": 0}]}),
        encoding="utf-8",
    )
    viewer_scene = output.parent.parent / "05_viewer_scene/sfm_viewer_scene.json"
    viewer_scene.parent.mkdir(parents=True, exist_ok=True)
    viewer_scene.write_text(
        json.dumps({"meta": {"workflow": "srt_full_pose"}}),
        encoding="utf-8",
    )

    result = adapter.validate_outputs(prepared)

    assert result.status == "success"
    assert result.outputs["trajectory"] == str(output)
    assert result.outputs["diagnostics"] == str(diagnostics)
    assert result.outputs["camera_path"] == str(camera_path)
    assert result.outputs["report"] == str(report)
    assert result.outputs["initial_camera_track"] == str(initial_track)
    assert result.outputs["viewer_scene"] == str(viewer_scene)
    assert result.validation_proof is not None
    assert result.validation_proof["metric_scale_locked"] is True
    assert result.validation_proof["initial_camera_track_sha256"]
    assert result.validation_proof["viewer_scene_sha256"]


@pytest.mark.parametrize(
    "missing_relative",
    (
        "03_alignment/camera_track_pred.json",
        "05_viewer_scene/sfm_viewer_scene.json",
    ),
)
def test_full_pose_adapter_rejects_missing_initial_workbench_artifact(
    tmp_path: Path,
    missing_relative: str,
) -> None:
    inputs = _full_pose_inputs(tmp_path)
    adapter = default_workflow_adapters().for_workflow("srt_full_pose")
    prepared = adapter.prepare_inputs(inputs)
    output = (
        inputs.attempt_directory
        / "02_srt_full_pose"
        / "camera_trajectory_full_pose.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "poses": [{"registered": True}],
                "meta": {
                    "trajectory_mode": "srt_full_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                },
            }
        ),
        encoding="utf-8",
    )
    for name, content in {
        "02_srt_full_pose/georeference_diagnostics.json": "{}",
        "02_srt_full_pose/camera_path_full_pose.csv": "frame_index\n0\n",
        "02_srt_full_pose/full_pose_report.md": "# report\n",
        "03_alignment/camera_track_pred.json": "{}",
        "05_viewer_scene/sfm_viewer_scene.json": "{}",
    }.items():
        path = inputs.attempt_directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (inputs.attempt_directory / missing_relative).unlink()

    result = adapter.validate_outputs(prepared)

    assert result.status == "failed"
    assert "full-pose artifact output is missing" in result.error


def _fixed_track_inputs(
    tmp_path: Path,
    *,
    include_fov: bool = True,
    reconstruction_resolution: str | None = "1080p",
) -> AdapterInputs:
    inputs = _full_pose_inputs(tmp_path)
    parameters = _full_pose_parameters()
    settings = {
        "horizontal_fov_deg": 72.0,
        "route_offset_xyz_m": [1.0, -2.0, 5.0],
    }
    if reconstruction_resolution is not None:
        settings["reconstruction_resolution"] = reconstruction_resolution
    if not include_fov:
        settings.pop("horizontal_fov_deg")
    parameters.pop("srt_full_pose")
    parameters["srt_fixed_track_visual_pose"] = settings
    return AdapterInputs(
        inputs.project_id,
        inputs.clip_id,
        inputs.video_path,
        inputs.srt_path,
        inputs.attempt_directory,
        parameters=parameters,
        source_start_pts=inputs.source_start_pts,
        source_end_pts_exclusive=inputs.source_end_pts_exclusive,
        source_time_base=inputs.source_time_base,
        frame_map_path=inputs.frame_map_path,
    )


def test_fixed_track_adapter_runs_colmap_before_pose_transfer(
    tmp_path: Path,
) -> None:
    inputs = _fixed_track_inputs(tmp_path)
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )

    commands = adapter.build_commands(adapter.prepare_inputs(inputs))

    assert adapter.version == "4"
    assert len(commands) == 3
    planner, sfm, transfer = commands
    assert planner[1:3] == ("-m", "cadscene.cli.plan_srt_adaptive_frames")
    assert sfm[1:3] == ("-m", "cadscene.cli.run_sfm")
    assert sfm[sfm.index("--backend") + 1] == "colmap_cli"
    assert sfm[sfm.index("--device") + 1] == "auto"
    assert sfm[sfm.index("--reconstruction-height") + 1] == "1080"
    assert sfm[sfm.index("--max-image-size") + 1] == "1920"
    assert sfm[sfm.index("--camera-model") + 1] == "RADIAL"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "1321.32664365,960,540,0,0"
    )
    assert "--no-refine-focal-length" not in sfm
    assert sfm[sfm.index("--max-num-features") + 1] == "8000"
    assert sfm[sfm.index("--sequential-overlap") + 1] == "10"
    assert sfm[sfm.index("--ba-global-max-num-iterations") + 1] == "15"
    assert sfm[sfm.index("--source-frames-file") + 1].endswith(
        "adaptive_frame_plan.json"
    )
    assert transfer[1:3] == (
        "-m",
        "cadscene.cli.build_srt_fixed_track_visual_pose",
    )
    assert transfer[transfer.index("--reconstruction-trajectory") + 1].endswith(
        "02_sfm\\camera_trajectory.json"
    )
    assert transfer[transfer.index("--sparse-ply") + 1].endswith(
        "02_sfm\\sparse_points.ply"
    )
    assert "cadscene.cli.fuse_srt_sfm" not in " ".join(
        item for command in commands for item in command
    )
    config_path = Path(transfer[transfer.index("--config") + 1])
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["build"]["horizontal_fov_deg"] == 72.0
    assert payload["build"]["reconstruction_resolution"] == "1080p"
    assert payload["build"]["route_offset_xyz_m"] == [1.0, -2.0, 5.0]


def test_fixed_track_720p_command_scales_intrinsics(tmp_path: Path) -> None:
    inputs = _fixed_track_inputs(
        tmp_path, reconstruction_resolution="720p"
    )
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )

    sfm = adapter.build_commands(adapter.prepare_inputs(inputs))[1]

    assert sfm[sfm.index("--reconstruction-height") + 1] == "720"
    assert sfm[sfm.index("--max-image-size") + 1] == "1280"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "880.884429102,640,360,0,0"
    )


def test_fixed_track_source_command_keeps_source_intrinsics(
    tmp_path: Path,
) -> None:
    inputs = _fixed_track_inputs(
        tmp_path, reconstruction_resolution="source"
    )
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )

    sfm = adapter.build_commands(adapter.prepare_inputs(inputs))[1]

    assert "--reconstruction-height" not in sfm
    assert sfm[sfm.index("--max-image-size") + 1] == "3840"
    assert sfm[sfm.index("--camera-params") + 1] == (
        "2642.6532873,1920,1080,0,0"
    )


def test_fixed_track_prepare_requires_user_fov(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="horizontal_fov_deg"):
        default_workflow_adapters().for_workflow(
            "srt_fixed_track_visual_pose"
        ).prepare_inputs(_fixed_track_inputs(tmp_path, include_fov=False))


def test_fixed_track_adapter_validates_route_without_sparse_points(
    tmp_path: Path,
) -> None:
    inputs = _fixed_track_inputs(tmp_path)
    adapter = default_workflow_adapters().for_workflow(
        "srt_fixed_track_visual_pose"
    )
    prepared = adapter.prepare_inputs(inputs)
    output = (
        inputs.attempt_directory
        / "02_srt_visual_pose"
        / "camera_trajectory_visual_pose.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "poses": [
                    {
                        "frame_index": 0,
                        "registered": True,
                        "position_available": True,
                        "orientation_available": False,
                        "center": [0.0, 0.0, 80.0],
                    }
                ],
                "meta": {
                    "trajectory_mode": "srt_fixed_track_visual_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                    "position_source": "srt_cad_locked",
                },
            }
        ),
        encoding="utf-8",
    )
    artifacts = {
        "camera_path": output.with_name("camera_path_srt_locked.csv"),
        "diagnostics": output.with_name("orientation_diagnostics.json"),
        "report": output.with_name("visual_pose_report.md"),
        "initial_camera_track": (
            inputs.attempt_directory / "03_alignment" / "camera_track_pred.json"
        ),
        "viewer_scene": (
            inputs.attempt_directory / "05_viewer_scene" / "sfm_viewer_scene.json"
        ),
        "calibration": output.with_name("camera_calibration.json"),
        "joint_alignment": output.with_name("joint_alignment.json"),
    }
    artifacts["camera_path"].write_text(
        "frame_index,camera_x,camera_y,camera_z\n0,0,0,80\n",
        encoding="utf-8",
    )
    artifacts["diagnostics"].write_text("{}", encoding="utf-8")
    artifacts["report"].write_text("# report\n", encoding="utf-8")
    artifacts["calibration"].write_text(
        json.dumps({"model": "RADIAL", "focal_px": 1000.0}), encoding="utf-8"
    )
    artifacts["joint_alignment"].write_text(
        json.dumps({"status": "success"}), encoding="utf-8"
    )
    artifacts["initial_camera_track"].parent.mkdir(parents=True, exist_ok=True)
    artifacts["initial_camera_track"].write_text("{}", encoding="utf-8")
    artifacts["viewer_scene"].parent.mkdir(parents=True, exist_ok=True)
    artifacts["viewer_scene"].write_text("{}", encoding="utf-8")

    result = adapter.validate_outputs(prepared)

    assert result.status == "success"
    assert result.outputs["trajectory"] == str(output)
    assert result.outputs["initial_camera_track"] == str(
        artifacts["initial_camera_track"]
    )
    assert result.outputs["viewer_scene"] == str(artifacts["viewer_scene"])
    assert result.outputs["calibration"] == str(artifacts["calibration"])
    assert result.outputs["joint_alignment"] == str(artifacts["joint_alignment"])
    assert "sparse_points" not in result.outputs
    assert result.validation_proof["position_source"] == "srt_cad_locked"


def test_pure_rotation_adapter_wraps_existing_cli(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    frame_map = tmp_path / "clip_frame_map.json"
    frame_map.write_text("{}", encoding="utf-8")
    inputs = AdapterInputs(
        "p1",
        "c1",
        video,
        None,
        tmp_path / "attempt-1",
        frame_map_path=frame_map,
    )
    adapter = default_workflow_adapters().for_workflow("pure_rotation")

    command = adapter.build_command(adapter.prepare_inputs(inputs))

    assert command[1:3] == ("-m", "cadscene.cli.run_pure_rotation")
    assert command[command.index("--frame-map") + 1] == str(frame_map)


def test_pure_rotation_adapter_version_invalidates_pre_pinned_calibration_jobs() -> None:
    adapter = default_workflow_adapters().for_workflow("pure_rotation")

    assert adapter.version == "2"


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
