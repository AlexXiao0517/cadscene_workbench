from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from cadscene.core.io import write_csv_utf8_sig, write_json
from cadscene.viewer.export_scene import (
    ExportViewerSceneConfig,
    build_viewer_scene,
    load_suggestions,
    prepare_point_cloud,
)
from cadscene.viewer.schema import validate_viewer_scene


def _write_ply(path: Path) -> None:
    rows = [
        "0 0 0 10 20 30",
        "1 0 0 40 50 60",
        "2 0 0 70 80 90",
        "3 0 0 100 110 120",
    ]
    path.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 4",
                "property float x",
                "property float y",
                "property float z",
                "property uchar red",
                "property uchar green",
                "property uchar blue",
                "end_header",
                *rows,
            ]
        ),
        encoding="utf-8",
    )


def _write_trajectory(path: Path) -> None:
    write_json(
        path,
        {
            "fps": 25.0,
            "width": 100,
            "height": 100,
            "intrinsics": [{"width": 100, "params": [50.0]}],
            "poses": [
                {"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
                {"frame_index": 10, "registered": True, "center": [1, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
            ],
        },
    )


def _write_alignment(path: Path) -> None:
    write_json(
        path,
        {
            "schema_version": "cadscene_alignment_v1",
            "sim3": {"scale": 2.0, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation": [10, 0, 0]},
            "residuals": {"frames": [0, 10], "position_m": [[100, 0, 0], [100, 0, 0]], "angle_deg": [[0, 0, 0], [0, 0, 0]]},
        },
    )


def _write_anchor_path(path: Path) -> None:
    write_csv_utf8_sig(
        path,
        [
            {"frame_index": 0, "camera_x": 100.0, "camera_y": 0.0, "camera_z": 2.0, "yaw": 0.0, "pitch": 12.0, "roll": 0.0, "fov": 70.0},
            {"frame_index": 10, "camera_x": 120.0, "camera_y": 0.0, "camera_z": 2.0, "yaw": 5.0, "pitch": 8.0, "roll": 0.0, "fov": 70.0},
        ],
    )


def test_point_cloud_sampling_and_global_sim3_without_anchor_residual(tmp_path: Path) -> None:
    ply = tmp_path / "points.ply"
    alignment = tmp_path / "alignment.json"
    _write_ply(ply)
    _write_alignment(alignment)
    config = ExportViewerSceneConfig(cad_scale=2.0, origin_xy=(1000.0, 2000.0), max_points=2, point_sample_mode="uniform")

    cloud = prepare_point_cloud(ply, alignment, config)

    assert cloud["count_original"] == 4
    assert cloud["count_exported"] == 2
    assert cloud["has_rgb"] is True
    assert cloud["rgb_range"] == "0_255"
    assert cloud["data"][0][:3] == [1005.0, 2000.0, 0.0]
    assert all(isinstance(v, int) and 0 <= v <= 255 for v in cloud["data"][0][3:6])
    assert cloud["data"][1][:3] == [1007.0, 2000.0, 0.0]


def test_build_scene_converts_tracks_suggestions_and_handles_optional_inputs(tmp_path: Path) -> None:
    ply = tmp_path / "points.ply"
    trajectory = tmp_path / "trajectory.json"
    alignment = tmp_path / "alignment.json"
    anchored = tmp_path / "sfm_camera_path.csv"
    suggestions = tmp_path / "suggestions.json"
    quality = tmp_path / "quality_timeline.csv"
    _write_ply(ply)
    _write_trajectory(trajectory)
    _write_alignment(alignment)
    _write_anchor_path(anchored)
    write_json(suggestions, {"suggestions": [{"frame_index": 10, "risk_score": 0.8, "risk_level": "high", "reason_codes": ["far_from_anchor"], "suggest_action": "建议添加关键帧"}]})
    write_csv_utf8_sig(quality, [{"frame_index": 10, "risk_score": 0.8}])

    scene, stats = build_viewer_scene(
        dataset="synthetic",
        run_id="stage3c",
        sparse_ply=ply,
        trajectory=trajectory,
        alignment=alignment,
        sfm_camera_path=anchored,
        quality_timeline=quality,
        suggestions=suggestions,
        config=ExportViewerSceneConfig(cad_scale=2.0, origin_xy=(1000.0, 2000.0), max_points=10),
    )

    validate_viewer_scene(scene)
    assert scene["meta"]["rgb_range"] == "0_255"
    assert scene["tracks"]["global_sfm_track"][0]["camera"]["x"] == 1005.0
    assert scene["tracks"]["anchored_camera_path"][0]["camera"]["x"] == 1050.0
    assert scene["tracks"]["anchored_camera_path"][0]["camera"]["pitch"] == -12.0
    assert scene["suggestions"][0]["frame_index"] == 10
    assert scene["quality"]["available"] is True
    assert stats["point_count_exported"] == 4
    assert stats["global_track_count"] == 2
    assert stats["anchored_track_count"] == 2


def test_suggestions_list_format_and_no_points_no_quality_are_valid(tmp_path: Path) -> None:
    suggestions = tmp_path / "suggestions.json"
    alignment = tmp_path / "alignment.json"
    _write_alignment(alignment)
    write_json(suggestions, [{"frame_index": 5, "priority": "high"}])

    assert load_suggestions(suggestions)[0]["frame_index"] == 5
    scene, stats = build_viewer_scene(
        dataset="synthetic",
        run_id="empty",
        sparse_ply=None,
        trajectory=None,
        alignment=alignment,
        sfm_camera_path=None,
        quality_timeline=None,
        suggestions=None,
        config=ExportViewerSceneConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )

    validate_viewer_scene(scene)
    assert scene["points"]["count_exported"] == 0
    assert scene["quality"]["available"] is False
    assert stats["suggestion_count"] == 0
