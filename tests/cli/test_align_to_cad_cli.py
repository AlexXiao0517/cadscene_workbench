from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.core.io import write_json


def _write_synthetic_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    trajectory = tmp_path / "camera_trajectory.json"
    write_json(
        trajectory,
        {
            "fps": 25.0,
            "width": 1280,
            "height": 720,
            "intrinsics": [{"width": 1280, "params": [640.0]}],
            "poses": [
                {"frame_index": 0, "registered": True, "center": [0.0, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
                {"frame_index": 10, "registered": True, "center": [1.0, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
                {"frame_index": 20, "registered": True, "center": [2.0, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]},
            ],
        },
    )
    track = tmp_path / "camera_track.json"
    write_json(
        track,
        {
            "keyframes": [
                {"frame": 0, "source": "manual_keyframe", "camera": {"x": 10.0, "y": 20.0, "z": 0.0, "yaw": 0.0, "pitch": -5.0, "roll": 0.0, "fov": 60.0}},
                {"frame": 10, "source": "confirmed_keyframe", "camera": {"x": 12.0, "y": 20.0, "z": 0.0, "yaw": 0.0, "pitch": -5.0, "roll": 0.0, "fov": 60.0}},
                {"frame": 20, "source": "manual_keyframe", "camera": {"x": 14.0, "y": 20.0, "z": 0.0, "yaw": 0.0, "pitch": -5.0, "roll": 0.0, "fov": 60.0}},
            ]
        },
    )
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    return trajectory, track, cad_dir


def test_align_to_cad_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.align_to_cad", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--web-camera-track" in result.stdout


def test_align_to_cad_cli_synthetic_smoke_writes_outputs_and_manifest(tmp_path: Path) -> None:
    trajectory, track, cad_dir = _write_synthetic_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.align_to_cad",
            "--dataset",
            "synthetic",
            "--run-id",
            "stage3a",
            "--output-root",
            str(output_root),
            "--trajectory",
            str(trajectory),
            "--web-camera-track",
            str(track),
            "--cad-dir",
            str(cad_dir),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--frame-step",
            "10",
            "--frontend-track-step",
            "10",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "synthetic" / "stage3a" / "03_alignment"
    for name in [
        "alignment.json",
        "sfm_camera_path.csv",
        "camera_track_pred.json",
        "keyframe_correspondences.csv",
        "alignment_report.md",
    ]:
        assert (stage_dir / name).exists()
    assert (stage_dir / "sfm_camera_path.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    manifest = json.loads((output_root / "synthetic" / "stage3a" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "alignment"
    assert manifest["stages"][-1]["status"] == "success"
