from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import subprocess

from cadscene.projects.adapters import AdapterInputs
from cadscene.projects.workflow_adapters import default_workflow_adapters


def _write_full_pose_srt(path: Path) -> None:
    blocks: list[str] = []
    for index, (start_ms, longitude) in enumerate(
        ((0, 120.0), (40, 120.00001), (80, 120.00002)),
        start=1,
    ):
        end_ms = start_ms + 40
        blocks.append(
            "\n".join(
                [
                    str(index),
                    (
                        f"00:00:00,{start_ms:03d} --> "
                        f"00:00:00,{end_ms:03d}"
                    ),
                    (
                        "[latitude: 30.0] "
                        f"[longitude: {longitude:.8f}] "
                        f"[rel_alt: {60.0 + index - 1:.1f}] "
                        "[gb_yaw: 0.0] [gb_pitch: -90.0] [gb_roll: 0.0]"
                    ),
                ]
            )
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _confirmed_georeference() -> dict[str, object]:
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


def test_srt_full_pose_runs_without_sfm_and_exports_no_pointcloud_scene(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    srt = tmp_path / "clip.srt"
    frame_map = tmp_path / "clip_frame_map.json"
    video.write_bytes(b"synthetic-mp4-placeholder")
    _write_full_pose_srt(srt)
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 25},
                "clips": [
                    {
                        "clip_id": "clip-1",
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 3,
                        "frames": [
                            {"ordinal": index, "pts": index}
                            for index in range(3)
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    attempt = tmp_path / "attempt"
    inputs = AdapterInputs(
        project_id="project-1",
        clip_id="clip-1",
        video_path=video,
        srt_path=srt,
        attempt_directory=attempt,
        parameters={
            "cad_georeference": _confirmed_georeference(),
            "srt_full_pose": {
                "horizontal_fov_deg": 82.0,
                "cad_z_offset_m": 0.0,
                "attitude_profile": "dji_absolute_ned",
            },
            "cad_origin_xy": [500000.0, 3320113.3978450196],
            "cad_scale": 1.0,
            "video_metadata": {"width": 3840, "height": 2160, "fps": 25.0},
        },
        source_start_pts=0,
        source_end_pts_exclusive=3,
        source_time_base=Fraction(1, 25),
        frame_map_path=frame_map,
    )
    adapter = default_workflow_adapters().for_workflow("srt_full_pose")
    prepared = adapter.prepare_inputs(inputs)
    commands = adapter.build_commands(prepared)
    modules = {
        command[index + 1]
        for command in commands
        for index, value in enumerate(command[:-1])
        if value == "-m"
    }

    assert modules == {"cadscene.cli.build_srt_full_pose"}
    completed = subprocess.run(
        commands[0], text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    result = adapter.validate_outputs(prepared)
    assert result.status == "success", result.error

    trajectory = Path(result.outputs["trajectory"])
    track = tmp_path / "camera_track_manual.json"
    track.write_text(json.dumps({"keyframes": []}), encoding="utf-8")
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    pipeline_command = [
        commands[0][0],
        "-m",
        "cadscene.cli.run_pipeline",
        "--config",
        str(Path.cwd() / "configs/pipelines/srt_full_pose_overlay.yaml"),
        "--dataset",
        inputs.project_id,
        "--run-id",
        inputs.clip_id,
        "--output-root",
        str(attempt),
        "--stages",
        "alignment,viewer_scene",
        "--trajectory",
        str(trajectory),
        "--web-camera-track",
        str(track),
        "--video",
        str(video),
        "--cad-dir",
        str(cad_dir),
        "--cad-scale",
        "1",
        "--origin-xy",
        "500000",
        "3320113.3978450196",
    ]
    assert "--sparse-ply" not in pipeline_command
    assert "cadscene.cli.run_sfm" not in pipeline_command
    completed = subprocess.run(
        pipeline_command, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stderr

    run = attempt / inputs.project_id / inputs.clip_id
    alignment = json.loads(
        (run / "03_alignment/alignment.json").read_text(encoding="utf-8")
    )
    scene = json.loads(
        (run / "05_viewer_scene/sfm_viewer_scene.json").read_text(
            encoding="utf-8"
        )
    )
    assert alignment["sim3"]["scale"] == 1.0
    assert alignment["validation"]["alignment_mode"] == "metric_direct"
    assert scene["meta"]["workflow"] == "srt_full_pose"
    assert scene["points"]["count_exported"] == 0
    assert scene["points"]["data"] == []
    assert len(scene["tracks"]["global_sfm_track"]) == 3
