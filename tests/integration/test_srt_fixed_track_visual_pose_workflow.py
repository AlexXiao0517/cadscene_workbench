from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import cadscene.cli.build_srt_fixed_track_visual_pose as builder
from cadscene.alignment.aligner import AlignmentConfig, run_alignment
from cadscene.srt.fixed_track_visual_pose import OrientationSolution
from cadscene.srt.georeference import CadGeoreference, project_wgs84_to_cad_raw


def test_progress_write_retries_a_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch,
) -> None:
    progress = tmp_path / "progress.json"
    original = builder._atomic_write_json
    calls = 0

    def flaky_write(path: Path, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(5, "sharing violation", str(path))
        original(path, value)

    monkeypatch.setattr(builder, "_atomic_write_json", flaky_write)
    monkeypatch.setattr(builder, "sleep", lambda _seconds: None, raising=False)

    builder._write_progress(progress, "parse_srt", "正在解析 SRT", 0.08)

    assert calls == 2
    assert json.loads(progress.read_text(encoding="utf-8"))["stage"] == "parse_srt"


def test_fixed_track_end_to_end_keeps_route_and_exports_no_point_cloud(
    tmp_path: Path, monkeypatch,
) -> None:
    pipeline = Path("configs/pipelines/srt_fixed_track_visual_pose_overlay.yaml")
    assert pipeline.is_file()

    georeference = CadGeoreference.from_dict(
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
            "source": "test_confirmed",
            "confirmed": True,
            "confidence": 1.0,
        }
    )
    origin = project_wgs84_to_cad_raw(120.0, 30.0, georeference)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video is replaced by a deterministic orientation solver")
    srt = tmp_path / "clip.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "[latitude:30] [longitude:120] [rel_alt:80] [abs_alt:230]\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n"
        "[latitude:30.00001] [longitude:120.00001] [rel_alt:81] [abs_alt:231]\n",
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
                        "source_end_pts_exclusive": 2000,
                        "frames": [{"pts": 0}, {"pts": 1000}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "video_metadata": {"width": 1920, "height": 1080, "fps": 1.0},
                "build": {
                    "schema_version": 1,
                    "clip_id": "clip-1",
                    "source_start_pts": 0,
                    "source_end_pts_exclusive": 2000,
                    "source_time_base": {"numerator": 1, "denominator": 1000},
                    "georeference": georeference.to_dict(),
                    "cad_origin_xy": list(origin),
                    "cad_scale": 1.0,
                    "horizontal_fov_deg": 72.0,
                    "route_offset_xyz_m": [2.0, -3.0, 4.0],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        builder,
        "estimate_video_orientations",
        lambda *_args, **_kwargs: OrientationSolution(
            status="orientation_partial",
            rotations={0: np.eye(3)},
            diagnostics=(),
            warnings=("frame 1 orientation unavailable",),
        ),
    )
    output_root = tmp_path / "runs"

    assert builder.main(
        [
            "--dataset", "project-1",
            "--run-id", "clip-1",
            "--output-root", str(output_root),
            "--video", str(video),
            "--srt", str(srt),
            "--frame-map", str(frame_map),
            "--config", str(config),
        ]
    ) == 0

    run_root = output_root / "project-1" / "clip-1"
    trajectory_path = run_root / "02_srt_visual_pose/camera_trajectory_visual_pose.json"
    track_path = run_root / "03_alignment/camera_track_pred.json"
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    scene = json.loads(
        (run_root / "05_viewer_scene/sfm_viewer_scene.json").read_text(
            encoding="utf-8"
        )
    )
    alignment = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )

    expected = np.asarray([pose["center"] for pose in trajectory["poses"]])
    actual = np.asarray(
        [
            [row["camera_x"], row["camera_y"], row["camera_z"]]
            for row in alignment.sfm_camera_path_rows
        ]
    )
    np.testing.assert_allclose(actual, expected, atol=1e-12)
    assert trajectory["meta"]["position_source"] == "srt_cad_locked"
    assert scene["points"]["count_exported"] == 0
    assert scene["tracks"]["global_sfm_track"]
    assert not tuple(run_root.rglob("*.ply"))
