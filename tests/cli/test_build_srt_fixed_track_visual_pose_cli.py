from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path

import cadscene.cli.build_srt_fixed_track_visual_pose as subject
import numpy as np
import pytest
from cadscene.srt.fixed_track_visual_pose import (
    FixedTrackPosition,
    FixedTrackVisualPoseConfig,
    OrientationSolution,
    build_fixed_track_positions,
)
from cadscene.terrain.context import TerrainContext
from cadscene.terrain.tpkg import TerrainControlSet, TerrainSourceSummary
from cadscene.srt.georeference import CadGeoreference, project_wgs84_to_cad_raw
from cadscene.srt.parser import load_srt_records


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


def _position(frame: int, x: float, *, rel_alt: float = 80.0) -> FixedTrackPosition:
    return FixedTrackPosition(
        frame_index=frame,
        source_pts=frame * 1000,
        pts_time_sec=float(frame),
        canonical_center=(x, 0.0, rel_alt),
        center=(x, 0.0, rel_alt),
        latitude=30.0,
        longitude=120.0,
        rel_alt=rel_alt,
        abs_alt=150.0 + rel_alt,
        projected_easting=x,
        projected_northing=0.0,
        cad_raw_x=x,
        cad_raw_y=0.0,
        interpolated=False,
        source_entry_before=0,
        source_entry_after=0,
    )


def test_first_uncovered_route_point_downgrades_terrain_datum_to_partial() -> None:
    summary = TerrainSourceSummary(
        source_id="a" * 64,
        path="terrain.tpkg",
        feature_count=1,
        vertex_count=1,
        geometry_types={"PointZ": 1},
        layers={"terrain": 1},
        bbox_lon_lat=(0.0, 0.0, 0.0, 0.0),
        z_range_m=(130.0, 130.0),
    )
    controls = TerrainControlSet(
        points_xyz=np.asarray([[500.0, 0.0, 130.0]]),
        point_source_ids=(summary.source_id,),
        point_labels=("",),
        point_colors_bgr=np.asarray([[0, 0, 0]], dtype="uint8"),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
        segment_source_ids=(),
        segment_colors_bgr=np.empty((0, 3), dtype="uint8"),
        segment_widths=np.empty(0, dtype="int32"),
        sources=(summary,),
        fingerprint="b" * 64,
    )
    context = TerrainContext(
        mode="terrain",
        coverage_fraction=0.95,
        covered_route_points=19,
        total_route_points=20,
        max_control_distance_m=160.0,
        height_range_m=(130.0, 130.0),
        source_fingerprints=(summary.source_id,),
        controls_fingerprint=controls.fingerprint,
        cad_fingerprint="cad",
        georeference_fingerprint="geo",
    )

    positions, downgraded = subject._align_relative_height_datum(
        (_position(0, 0.0), _position(1, 500.0)), controls, context
    )

    assert downgraded.mode == "partial"
    assert downgraded.reference_ground_m is None
    assert downgraded.camera_height_datum_valid is False
    assert positions[0].center[2] == 80.0


def test_in_flight_recording_does_not_treat_first_terrain_height_as_home_datum() -> None:
    summary = TerrainSourceSummary(
        source_id="a" * 64,
        path="terrain.tpkg",
        feature_count=1,
        vertex_count=1,
        geometry_types={"PointZ": 1},
        layers={"terrain": 1},
        bbox_lon_lat=(0.0, 0.0, 0.0, 0.0),
        z_range_m=(130.0, 130.0),
    )
    controls = TerrainControlSet(
        points_xyz=np.asarray([[0.0, 0.0, 130.0], [10.0, 0.0, 131.0]]),
        point_source_ids=(summary.source_id, summary.source_id),
        point_labels=("", ""),
        point_colors_bgr=np.zeros((2, 3), dtype="uint8"),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
        segment_source_ids=(),
        segment_colors_bgr=np.empty((0, 3), dtype="uint8"),
        segment_widths=np.empty(0, dtype="int32"),
        sources=(summary,),
        fingerprint="b" * 64,
    )
    context = TerrainContext(
        mode="terrain",
        coverage_fraction=1.0,
        covered_route_points=2,
        total_route_points=2,
        max_control_distance_m=160.0,
        height_range_m=(130.0, 131.0),
        source_fingerprints=(summary.source_id,),
        controls_fingerprint=controls.fingerprint,
        cad_fingerprint="cad",
        georeference_fingerprint="geo",
    )

    positions, downgraded = subject._align_relative_height_datum(
        (_position(0, 0.0), _position(1, 10.0, rel_alt=81.0)), controls, context
    )

    assert downgraded.mode == "partial"
    assert downgraded.camera_height_datum_valid is False
    assert downgraded.cad_fallback_ground_m == pytest.approx(130.5)
    assert [item.center[2] for item in positions] == [80.0, 81.0]


def test_near_zero_sample_without_verified_home_still_requires_vertical_calibration() -> None:
    summary = TerrainSourceSummary(
        source_id="a" * 64,
        path="terrain.tpkg",
        feature_count=1,
        vertex_count=1,
        geometry_types={"PointZ": 1},
        layers={"terrain": 1},
        bbox_lon_lat=(0.0, 0.0, 0.0, 0.0),
        z_range_m=(130.0, 130.0),
    )
    controls = TerrainControlSet(
        points_xyz=np.asarray([[0.0, 0.0, 130.0], [10.0, 0.0, 131.0]]),
        point_source_ids=(summary.source_id, summary.source_id),
        point_labels=("", ""),
        point_colors_bgr=np.zeros((2, 3), dtype="uint8"),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
        segment_source_ids=(),
        segment_colors_bgr=np.empty((0, 3), dtype="uint8"),
        segment_widths=np.empty(0, dtype="int32"),
        sources=(summary,),
        fingerprint="b" * 64,
    )
    context = TerrainContext(
        mode="terrain",
        coverage_fraction=1.0,
        covered_route_points=2,
        total_route_points=2,
        max_control_distance_m=160.0,
        height_range_m=(130.0, 131.0),
        source_fingerprints=(summary.source_id,),
        controls_fingerprint=controls.fingerprint,
        cad_fingerprint="cad",
        georeference_fingerprint="geo",
    )

    positions, aligned = subject._align_relative_height_datum(
        (_position(0, 0.0, rel_alt=0.5), _position(1, 10.0, rel_alt=80.0)),
        controls,
        context,
    )

    assert aligned.mode == "partial"
    assert aligned.camera_height_datum_valid is False
    assert aligned.reference_ground_m is None
    assert [item.center[2] for item in positions] == pytest.approx([0.5, 80.0])


def test_render_path_requires_a_pose_for_every_authoritative_frame() -> None:
    positions = (_position(0, 0.0), _position(2, 2.0))
    solution = OrientationSolution(
        status="orientation_partial",
        rotations={0: np.eye(3)},
    )

    with pytest.raises(ValueError, match="complete per-frame"):
        subject._require_complete_render_path(
            positions,
            solution,
            {
                "clips": [
                    {
                        "clip_id": "clip-1",
                        "frames": [
                            {"ordinal": 0, "pts": 0},
                            {"ordinal": 1, "pts": 1000},
                            {"ordinal": 2, "pts": 2000},
                        ],
                    }
                ]
            },
            "clip-1",
        )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    srt = tmp_path / "flight.srt"
    srt.write_text(
        "\n".join(
            [
                f"{index + 1}\n00:00:0{index},000 --> 00:00:0{index},100\n"
                f"[latitude: {[30.0, 30.00001, 30.00003][index]}] "
                f"[longitude: {[120.0, 120.00002, 120.00001][index]}] "
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


def _write_reconstruction_inputs(
    tmp_path: Path,
    srt: Path,
    frame_map: Path,
) -> tuple[Path, Path]:
    positions = build_fixed_track_positions(
        load_srt_records(srt),
        json.loads(frame_map.read_text(encoding="utf-8")),
        _config(),
    )
    trajectory = tmp_path / "reconstruction.json"
    trajectory.write_text(
        json.dumps(
            {
                "fps": 1.0,
                "width": 1920,
                "height": 1080,
                "intrinsics": [
                    {
                        "model": "RADIAL",
                        "width": 1920,
                        "height": 1080,
                        "params": [1321.32664365, 960.0, 540.0, -0.01, 0.04],
                    }
                ],
                "poses": [
                    {
                        "frame_index": item.frame_index,
                        "registered": True,
                        "center": list(item.center),
                        "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                    }
                    for item in positions
                ],
            }
        ),
        encoding="utf-8",
    )
    sparse = tmp_path / "sparse_points.ply"
    sparse.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n0 0 0 10 20 30\n",
        encoding="ascii",
    )
    return trajectory, sparse


def test_cli_transfers_colmap_attitude_and_publishes_diagnostic_point_cloud(
    tmp_path: Path,
) -> None:
    video, srt, frame_map, config = _write_inputs(tmp_path)
    reconstruction, sparse = _write_reconstruction_inputs(
        tmp_path, srt, frame_map
    )
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
            "--reconstruction-trajectory",
            str(reconstruction),
            "--sparse-ply",
            str(sparse),
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
    assert trajectory["meta"]["orientation_status"] == "ready"
    assert trajectory["meta"]["orientation_source"] == "colmap_sparse_srt_aligned"
    assert trajectory["meta"]["pose_prior_schema"] == "srt_pose_prior_v1"
    assert trajectory["poses"][0]["center"] == [1.0, -2.0, 85.0]
    assert trajectory["poses"][2]["position_available"] is True
    assert trajectory["poses"][2]["orientation_available"] is True
    assert trajectory["poses"][2]["registered"] is True
    assert "cam_from_world_quat_wxyz" in trajectory["poses"][2]
    assert trajectory["poses"][2]["visual_component_id"] == 0
    assert trajectory["poses"][2]["cam_from_visual_local_quat_wxyz"] == pytest.approx(
        [1.0, 0.0, 0.0, 0.0], abs=1e-12
    )
    assert trajectory["meta"]["relative_orientation_count"] == 3
    assert trajectory["meta"]["relative_orientation_coverage"] == 1.0
    assert trajectory["meta"]["recommended_anchor_frame"] == 1
    assert trajectory["meta"]["recommended_anchor_source_pts"] == 1000
    assert trajectory["meta"]["recommended_anchor_time_sec"] == 1.0
    track_path = run_root / "03_alignment" / "camera_track_pred.json"
    track = json.loads(track_path.read_text(encoding="utf-8"))
    assert len(track["keyframes"]) == 3
    scene_path = run_root / "05_viewer_scene" / "sfm_viewer_scene.json"
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    assert scene["points"]["count_exported"] == 1
    assert len(scene["tracks"]["global_sfm_track"]) == 3
    assert scene["tracks"]["global_sfm_track"][2]["orientation_available"] is True
    assert scene["meta"]["recommended_anchor_frame"] == 1
    assert scene["meta"]["source_video_size"] == [3840, 2160]
    assert scene["meta"]["reconstruction_image_size"] == [1920, 1080]
    assert scene["meta"]["reconstruction_resolution"] == "1080p"
    diagnostics = json.loads(
        (run_root / "02_srt_visual_pose/orientation_diagnostics.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(diagnostics["phase_timings_seconds"]) == {
        "parse_inputs",
        "project_srt_track",
        "transfer_colmap_attitude",
    }
    assert all(
        value >= 0.0 for value in diagnostics["phase_timings_seconds"].values()
    )
    calibration = json.loads(
        (run_root / "02_srt_visual_pose/camera_calibration.json").read_text(
            encoding="utf-8"
        )
    )
    assert calibration["model"] == "RADIAL"
    assert calibration["source_size"] == [3840, 2160]
    assert calibration["focal_px"] == pytest.approx(2642.6532873)
    assert calibration["cx_px"] == pytest.approx(1920.0)
    assert calibration["cy_px"] == pytest.approx(1080.0)
    assert calibration["k1"] == pytest.approx(-0.01)
    assert calibration["k2"] == pytest.approx(0.04)
    joint = json.loads(
        (run_root / "02_srt_visual_pose/joint_alignment.json").read_text(
            encoding="utf-8"
        )
    )
    assert joint["position_constraint"] == "SRT projected CAD-local"
    assert joint["dji_prior_available"] is False
    terrain = json.loads(
        (run_root / "02_srt_visual_pose/terrain_context.json").read_text(
            encoding="utf-8"
        )
    )
    assert terrain["terrain_mode"] == "relative"
    assert (run_root / "02_srt_visual_pose/terrain_controls.npz").is_file()
    assert scene["meta"]["terrain_mode"] == "relative"
    report = (
        run_root / "02_srt_visual_pose/visual_pose_report.md"
    ).read_text(encoding="utf-8")
    assert "COLMAP 稀疏三维重建" in report
    assert "最终相机中心逐帧强制采用 SRT" in report
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    assert progress_payload["stage"] == "completed"
    assert progress_payload["fraction"] == 1.0
    assert progress_payload["message"] == "SRT 轨迹与重建姿态已生成"


def test_cli_failure_does_not_publish_partial_route(tmp_path: Path) -> None:
    video, srt, frame_map, config = _write_inputs(tmp_path)
    reconstruction, _sparse = _write_reconstruction_inputs(tmp_path, srt, frame_map)
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
            "--reconstruction-trajectory",
            str(reconstruction),
        ]
    )

    assert exit_code == 1
    assert not (output_root / "p1" / "clip-1" / "02_srt_visual_pose").exists()
