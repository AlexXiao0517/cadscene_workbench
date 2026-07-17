from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.core.io import write_csv_utf8_sig, write_json


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, Path]:
    ply = tmp_path / "points.ply"
    ply.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 2",
                "property float x",
                "property float y",
                "property float z",
                "property uchar red",
                "property uchar green",
                "property uchar blue",
                "end_header",
                "0 0 0 255 0 0",
                "1 0 0 0 255 0",
            ]
        ),
        encoding="utf-8",
    )
    trajectory = tmp_path / "trajectory.json"
    write_json(
        trajectory,
        {
            "fps": 25,
            "poses": [
                {"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
                {"frame_index": 10, "registered": True, "center": [1, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
            ],
        },
    )
    alignment = tmp_path / "alignment.json"
    write_json(alignment, {"schema_version": "cadscene_alignment_v1", "sim3": {"scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation": [0, 0, 0]}})
    path_csv = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(path_csv, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 3, "roll": 0, "fov": 70}])
    quality = tmp_path / "quality_timeline.csv"
    write_csv_utf8_sig(quality, [{"frame_index": 0, "risk_score": 0.2}])
    suggestions = tmp_path / "suggestions.json"
    write_json(suggestions, {"suggestions": [{"frame_index": 0, "risk_score": 0.7, "risk_level": "high"}]})
    return ply, trajectory, alignment, path_csv, quality, suggestions


def test_export_viewer_scene_help_runs() -> None:
    result = subprocess.run([sys.executable, "-m", "cadscene.cli.export_viewer_scene", "--help"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "--sparse-ply" in result.stdout


def test_export_viewer_scene_cli_smoke_and_manifest(tmp_path: Path) -> None:
    ply, trajectory, alignment, path_csv, quality, suggestions = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.export_viewer_scene",
            "--dataset",
            "synthetic",
            "--run-id",
            "stage3c",
            "--output-root",
            str(output_root),
            "--sparse-ply",
            str(ply),
            "--trajectory",
            str(trajectory),
            "--alignment",
            str(alignment),
            "--sfm-camera-path",
            str(path_csv),
            "--quality-timeline",
            str(quality),
            "--suggestions",
            str(suggestions),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--max-points",
            "10",
            "--point-sample-mode",
            "voxel",
            "--voxel-size",
            "0.5",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "synthetic" / "stage3c" / "05_viewer_scene"
    assert (stage_dir / "sfm_viewer_scene.json").exists()
    assert (stage_dir / "sfm_viewer_scene_stats.json").exists()
    assert (stage_dir / "sfm_viewer_scene_report.md").exists()
    manifest = json.loads((output_root / "synthetic" / "stage3c" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "viewer_scene"


def test_export_viewer_scene_cli_allows_missing_suggestions_and_quality(tmp_path: Path) -> None:
    ply, trajectory, alignment, path_csv, _quality, _suggestions = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.export_viewer_scene",
            "--dataset",
            "synthetic",
            "--run-id",
            "missing_optional",
            "--output-root",
            str(output_root),
            "--sparse-ply",
            str(ply),
            "--trajectory",
            str(trajectory),
            "--alignment",
            str(alignment),
            "--sfm-camera-path",
            str(path_csv),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    scene = json.loads((output_root / "synthetic" / "missing_optional" / "05_viewer_scene" / "sfm_viewer_scene.json").read_text(encoding="utf-8"))
    assert scene["suggestions"] == []
    assert scene["quality"]["available"] is False
