from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from cadscene.core.io import write_csv_utf8_sig, write_json


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    import cv2

    video = tmp_path / "video.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for _ in range(3):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    write_json(cad_dir / "road_center.json", [{"points": [[0, 4], [1, 4]]}])
    path_csv = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(
        path_csv,
        [
            {"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70},
            {"frame_index": 2, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70},
        ],
    )
    return video, cad_dir, path_csv


def test_render_overlay_help_runs() -> None:
    result = subprocess.run([sys.executable, "-m", "cadscene.cli.render_overlay", "--help"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "--faded-overlay" in result.stdout


def test_render_overlay_cli_smoke_outputs_manifest(tmp_path: Path) -> None:
    video, cad_dir, path_csv = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.render_overlay",
            "--dataset",
            "synthetic",
            "--run-id",
            "stage3e",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--cad-dir",
            str(cad_dir),
            "--sfm-camera-path",
            str(path_csv),
            "--debug-scale",
            "1.0",
            "--overlay-linewidth",
            "2",
            "--overlay-alpha",
            "0.8",
            "--faded-overlay",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "synthetic" / "stage3e" / "08_render"
    assert (stage_dir / "sfm_align_overlay.mp4").exists()
    assert (stage_dir / "render_report.md").exists()
    assert (stage_dir / "render_stats.json").exists()
    manifest = json.loads((output_root / "synthetic" / "stage3e" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "render"


def test_render_overlay_cli_missing_input_has_clear_error(tmp_path: Path) -> None:
    video, cad_dir, _path_csv = _write_inputs(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.render_overlay",
            "--dataset",
            "synthetic",
            "--run-id",
            "missing",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(video),
            "--cad-dir",
            str(cad_dir),
            "--sfm-camera-path",
            str(tmp_path / "missing.csv"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "sfm_camera_path not found" in result.stderr
