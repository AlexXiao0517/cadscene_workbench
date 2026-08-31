from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.core.io import write_csv_utf8_sig, write_json


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    path_csv = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(
        path_csv,
        [
            {"frame_index": 0, "camera_x": 0.0, "camera_y": 0.0, "camera_z": 1.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
            {"frame_index": 40, "camera_x": 3.0, "camera_y": 0.0, "camera_z": 1.0, "yaw": 5.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
            {"frame_index": 80, "camera_x": 6.0, "camera_y": 1.0, "camera_z": 1.0, "yaw": 25.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
        ],
    )
    alignment = tmp_path / "alignment.json"
    write_json(
        alignment,
        {
            "schema_version": "cadscene_alignment_v1",
            "residuals": {"frames": [0, 40, 80], "position_m": [[0, 0, 0], [4, 0, 0], [8, 0, 0]], "angle_deg": [[0, 0, 0], [2, 0, 0], [4, 0, 0]]},
        },
    )
    track = tmp_path / "camera_track.json"
    write_json(
        track,
        {
            "keyframes": [
                {"frame": 0, "source": "manual_keyframe", "camera": {"x": 0, "y": 0, "z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}},
                {"frame": 40, "source": "algorithm_prediction", "camera": {"x": 3, "y": 0, "z": 1, "yaw": 5, "pitch": 0, "roll": 0, "fov": 70}},
            ]
        },
    )
    trajectory = tmp_path / "camera_trajectory.json"
    write_json(
        trajectory,
        {
            "fps": 25.0,
            "poses": [
                {"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
                {"frame_index": 40, "registered": True, "center": [1, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
                {"frame_index": 80, "registered": True, "center": [2, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
            ],
        },
    )
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    return path_csv, alignment, track, trajectory, cad_dir


def test_evaluate_quality_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.evaluate_quality", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--quality-mode" in result.stdout


def test_evaluate_quality_cli_synthetic_smoke_writes_outputs_and_manifest(tmp_path: Path) -> None:
    path_csv, alignment, track, trajectory, cad_dir = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    progress_file = tmp_path / "quality-progress.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.evaluate_quality",
            "--dataset",
            "synthetic",
            "--run-id",
            "stage3b",
            "--output-root",
            str(output_root),
            "--sfm-camera-path",
            str(path_csv),
            "--alignment",
            str(alignment),
            "--web-camera-track",
            str(track),
            "--trajectory",
            str(trajectory),
            "--cad-dir",
            str(cad_dir),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--quality-mode",
            "qa",
            "--no-suggestion-samples",
            "--progress-file",
            str(progress_file),
            "--progress-start",
            "0.1",
            "--progress-end",
            "0.55",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "synthetic" / "stage3b" / "04_quality"
    assert (stage_dir / "quality_timeline.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    assert (stage_dir / "keyframe_suggestions.json").exists()
    assert (stage_dir / "quality_report.md").exists()
    assert (stage_dir / "camera_track_pred_quality.json").exists()
    timeline_text = (stage_dir / "quality_timeline.csv").read_text(encoding="utf-8-sig")
    assert "visual_residual_risk" in timeline_text
    assert "unavailable" in timeline_text
    manifest = json.loads((output_root / "synthetic" / "stage3b" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "quality"
    assert manifest["stages"][-1]["status"] == "success"
    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    assert progress == {
        "schema_version": "1.0",
        "stage": "quality_outputs",
        "message": "质量评估产物已生成",
        "fraction": 0.55,
    }
