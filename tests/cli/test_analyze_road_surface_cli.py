from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.core.io import write_csv_utf8_sig, write_json


def _write_inputs(tmp_path: Path, *, sparse: bool = False) -> tuple[Path, Path, Path, Path, Path, Path]:
    ply = tmp_path / "points.ply"
    rows = ["0 0 0 10 20 30", "5 0 0.2 40 50 60"] if not sparse else ["0 30 8 10 20 30"]
    ply.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                f"element vertex {len(rows)}",
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
    trajectory = tmp_path / "trajectory.json"
    write_json(trajectory, {"fps": 25, "poses": [{"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]}]})
    alignment = tmp_path / "alignment.json"
    write_json(alignment, {"schema_version": "cadscene_alignment_v1", "sim3": {"scale": 1, "rotation": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "translation": [0, 0, 0]}})
    path_csv = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(path_csv, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 2, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}])
    track = tmp_path / "track.json"
    write_json(track, {"keyframes": [{"frame": 0, "source": "manual_keyframe", "camera": {"x": 0, "y": 0, "z": 2, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}}]})
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    write_json(cad_dir / "road_center.json", [{"points": [[0, 0], [10, 0]]}])
    return ply, trajectory, alignment, path_csv, track, cad_dir


def test_analyze_road_surface_help_runs() -> None:
    result = subprocess.run([sys.executable, "-m", "cadscene.cli.analyze_road_surface", "--help"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "--station-bin-m" in result.stdout


def test_analyze_road_surface_cli_smoke_outputs_manifest_and_warning(tmp_path: Path) -> None:
    ply, trajectory, alignment, path_csv, track, cad_dir = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.analyze_road_surface",
            "--dataset",
            "synthetic",
            "--run-id",
            "stage3d",
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
            "--web-camera-track",
            str(track),
            "--cad-dir",
            str(cad_dir),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--station-bin-m",
            "10",
            "--road-corridor-width",
            "5",
            "--export-viewer-scene",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "synthetic" / "stage3d" / "06_road_surface"
    assert (stage_dir / "sfm_geometry_report.md").exists()
    assert (stage_dir / "sfm_geometry_summary.json").exists()
    assert (stage_dir / "road_surface_profile.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert (stage_dir / "viewer_diagnostics_scene.json").exists()
    assert "semantic road point extraction unavailable" in (stage_dir / "sfm_geometry_report.md").read_text(encoding="utf-8")
    manifest = json.loads((output_root / "synthetic" / "stage3d" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "road_surface"


def test_analyze_road_surface_cli_insufficient_points_does_not_fail(tmp_path: Path) -> None:
    ply, trajectory, alignment, path_csv, track, cad_dir = _write_inputs(tmp_path, sparse=True)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.analyze_road_surface",
            "--dataset",
            "synthetic",
            "--run-id",
            "insufficient",
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
            "--web-camera-track",
            str(track),
            "--cad-dir",
            str(cad_dir),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--road-corridor-width",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads((output_root / "synthetic" / "insufficient" / "06_road_surface" / "sfm_geometry_summary.json").read_text(encoding="utf-8"))
    assert summary["conclusions"]["road_surface_reliability"] == "insufficient"
