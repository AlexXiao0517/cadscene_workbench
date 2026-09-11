from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from cadscene.core.io import write_csv_utf8_sig, write_json
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


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
            {"frame_index": 1, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70},
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


def test_calibrated_render_cli_uses_radial_terrain_contract(tmp_path: Path) -> None:
    try:
        ffmpeg = resolve_ffmpeg_executable()
    except (FileNotFoundError, RuntimeError):
        import pytest

        pytest.skip("H.264 FFmpeg is unavailable")
    video, cad_dir, path_csv = _write_inputs(tmp_path)
    calibration = tmp_path / "camera_calibration.json"
    write_json(
        calibration,
        {
            "source_size": [64, 48],
            "focal_px": 50.0,
            "cx_px": 32.0,
            "cy_px": 24.0,
            "k1": 0.0,
            "k2": 0.0,
        },
    )
    terrain_context = tmp_path / "terrain_context.json"
    write_json(terrain_context, {"terrain_mode": "relative", "terrain_coverage": 0.0})
    terrain_controls = tmp_path / "terrain_controls.npz"
    np.savez_compressed(
        terrain_controls,
        points_xyz=np.empty((0, 3)),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
    )
    output_root = tmp_path / "runs"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.render_overlay",
            "--dataset",
            "synthetic",
            "--run-id",
            "calibrated",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--cad-dir",
            str(cad_dir),
            "--sfm-camera-path",
            str(path_csv),
            "--camera-calibration",
            str(calibration),
            "--terrain-context",
            str(terrain_context),
            "--terrain-controls",
            str(terrain_controls),
            "--output-resolution",
            "source",
            "--ffmpeg",
            str(ffmpeg),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stats = json.loads(
        (
            output_root
            / "synthetic"
            / "calibrated"
            / "08_render"
            / "render_stats.json"
        ).read_text(encoding="utf-8")
    )
    assert stats["camera_interpolation"] == "exact_frame"
    assert stats["output_resolution"] == "source"
    assert stats["rendered_frame_count"] == 3
