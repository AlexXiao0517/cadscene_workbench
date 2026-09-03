from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path

import numpy as np

import cadscene.cli.build_srt_fixed_track_visual_pose as subject
from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackVisualPoseConfig,
    OrientationSolution,
)
from cadscene.srt.georeference import CadGeoreference, project_wgs84_to_cad_raw


def _georeference() -> CadGeoreference:
    return CadGeoreference.from_dict(
        {
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
            "confidence": 1.0,
        }
    )


def _config() -> FixedTrackVisualPoseConfig:
    georeference = _georeference()
    origin = project_wgs84_to_cad_raw(120.0, 30.0, georeference)
    return FixedTrackVisualPoseConfig(
        clip_id="clip-1",
        source_start_pts=0,
        source_end_pts_exclusive=2001,
        source_time_base=Fraction(1, 1000),
        georeference=georeference,
        cad_origin_xy=origin,
        cad_scale=1.0,
        horizontal_fov_deg=72.0,
        route_offset_xyz_m=(1.0, -2.0, 5.0),
    )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    srt = tmp_path / "flight.srt"
    srt.write_text(
        "\n".join(
            [
                f"{index + 1}\n00:00:0{index},000 --> 00:00:0{index},100\n"
                f"[latitude: {30.0 + index * 0.00001}] "
                f"[longitude: {120.0 + index * 0.00001}] "
                f"[rel_alt: {80.0 + index}] [abs_alt: {230.0 + index}]\n"
                for index in range(3)
            ]
        ),
        encoding="utf-8",
    )
    frame_map = tmp_path / "frame-map.json"
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "clips": [
                    {
                        "clip_id": "clip-1",
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 2001,
                        "frames": [
                            {"ordinal": 0, "pts": 0},
                            {"ordinal": 1, "pts": 1000},
                            {"ordinal": 2, "pts": 2000},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"video decoding is isolated by the test double")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "video_metadata": {"width": 3840, "height": 2160, "fps": 1.0},
                "build": _config().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    return video, srt, frame_map, config


def test_cli_publishes_fixed_route_and_route_first_workbench_without_point_cloud(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video, srt, frame_map, config = _write_inputs(tmp_path)

    def fake_estimate(_video, positions, _intrinsics, _config, **_kwargs):
        return OrientationSolution(
            status="orientation_partial",
            rotations={0: np.eye(3), 1: np.eye(3)},
            relative_rotations={
                0: np.eye(3),
                1: np.eye(3),
                2: np.eye(3),
            },
            component_ids={0: 0, 1: 0, 2: 0},
            recommended_anchor_frame=1,
            diagnostics=({"status": "accepted", "first_frame": 0, "second_frame": 1},),
            warnings=("frame 2 orientation unavailable",),
        )

    monkeypatch.setattr(subject, "estimate_video_orientations", fake_estimate)
    output_root = tmp_path / "runs"
    progress = tmp_path / "adapter-progress.json"

    exit_code = subject.main(
        [
            "--dataset",
            "p1",
            "--run-id",
            "clip-1",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--srt",
            str(srt),
            "--frame-map",
            str(frame_map),
            "--config",
            str(config),
            "--progress-file",
            str(progress),
        ]
    )

    run_root = output_root / "p1" / "clip-1"
    trajectory_path = (
        run_root / "02_srt_visual_pose" / "camera_trajectory_visual_pose.json"
    )
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert trajectory["meta"]["position_source"] == "srt_cad_locked"
    assert trajectory["meta"]["orientation_status"] == "orientation_partial"
    assert trajectory["poses"][0]["center"] == [1.0, -2.0, 85.0]
    assert trajectory["poses"][2]["position_available"] is True
    assert trajectory["poses"][2]["orientation_available"] is False
    assert trajectory["poses"][2]["registered"] is False
    assert "cam_from_world_quat_wxyz" not in trajectory["poses"][2]
    assert trajectory["poses"][2]["visual_component_id"] == 0
    assert trajectory["poses"][2]["cam_from_visual_local_quat_wxyz"] == [
        1.0,
        0.0,
        0.0,
        0.0,
    ]
    assert trajectory["meta"]["relative_orientation_count"] == 3
    assert trajectory["meta"]["relative_orientation_coverage"] == 1.0
    assert trajectory["meta"]["recommended_anchor_frame"] == 1
    assert trajectory["meta"]["recommended_anchor_source_pts"] == 1000
    assert trajectory["meta"]["recommended_anchor_time_sec"] == 1.0
    track_path = run_root / "03_alignment" / "camera_track_pred.json"
    track = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(track["keyframes"]) == 2
    scene_path = run_root / "05_viewer_scene" / "sfm_viewer_scene.json"
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["points"]["count_exported"] == 0
    assert len(scene["tracks"]["global_sfm_track"]) == 3
    assert scene["tracks"]["global_sfm_track"][2]["orientation_available"] is False
    assert scene["meta"]["recommended_anchor_frame"] == 1
    assert not tuple(run_root.rglob("*.ply"))
    diagnostics = json.loads(
        (run_root / "02_srt_visual_pose/orientation_diagnostics.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(diagnostics["phase_timings_seconds"]) == {
        "parse_inputs",
        "project_srt_track",
        "estimate_visual_attitude",
    }
    assert all(
        value >= 0.0 for value in diagnostics["phase_timings_seconds"].values()
    )
    report = (
        run_root / "02_srt_visual_pose/visual_pose_report.md"
    ).read_text(encoding="utf-8")
    assert "跳过位置注册、三角化、BA 和点云维护" in report
    assert "仅不写 PLY 并不是主要加速来源" in report
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    assert progress_payload["stage"] == "completed"
    assert progress_payload["fraction"] == 1.0
    assert progress_payload["message"] == "SRT 轨迹与视觉姿态已生成"


def test_cli_failure_does_not_publish_partial_route(tmp_path: Path) -> None:
    video, srt, frame_map, config = _write_inputs(tmp_path)
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["build"]["clip_id"] = "wrong-clip"
    config.write_text(json.dumps(payload), encoding="utf-8")
    output_root = tmp_path / "runs"

    exit_code = subject.main(
        [
            "--dataset",
            "p1",
            "--run-id",
            "clip-1",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--srt",
            str(srt),
            "--frame-map",
            str(frame_map),
            "--config",
            str(config),
        ]
    )

    assert exit_code == 1
    assert not (output_root / "p1" / "clip-1" / "02_srt_visual_pose").exists()
