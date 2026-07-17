from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.srt.fusion import build_fused_trajectory_json, fuse_positions


def test_fused_trajectory_runs_existing_align_to_cad(tmp_path: Path) -> None:
    raw = {
        "fps": 25.0,
        "width": 1280,
        "height": 720,
        "intrinsics": [{"width": 1280, "params": [640.0]}],
        "poses": [
            {"frame_index": index * 10, "registered": True, "center": [float(index), 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]}
            for index in range(3)
        ],
    }
    fused = build_fused_trajectory_json(
        raw,
        fuse_positions(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]],
            frame_times_sec=[0.0, 1.0, 2.0],
            srt_positions=[[10.0, 20.0, 0.0], [11.0, 20.0, 0.0], [12.0, 20.0, 0.0]],
            srt_valid=[True, True, True],
            min_smoothing_support=1,
        ),
        sim3_rotation=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        coordinate_meta={"horizontal_datum": "WGS84 local ENU", "up_source": "rel_alt_relative"},
    )
    trajectory = tmp_path / "camera_trajectory_fused.json"
    trajectory.write_text(json.dumps(fused), encoding="utf-8")
    track = tmp_path / "track.json"
    track.write_text(json.dumps({"keyframes": [{"frame": 0, "camera": {"x": 10, "y": 20, "z": 0, "yaw": 0, "pitch": -5, "roll": 0, "fov": 60}}, {"frame": 10, "camera": {"x": 11, "y": 20, "z": 0, "yaw": 0, "pitch": -5, "roll": 0, "fov": 60}}, {"frame": 20, "camera": {"x": 12, "y": 20, "z": 0, "yaw": 0, "pitch": -5, "roll": 0, "fov": 60}}]}), encoding="utf-8")
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    output = tmp_path / "runs"

    result = subprocess.run([sys.executable, "-m", "cadscene.cli.align_to_cad", "--dataset", "demo", "--run-id", "r1", "--output-root", str(output), "--trajectory", str(trajectory), "--web-camera-track", str(track), "--cad-dir", str(cad_dir), "--cad-scale", "1", "--origin-xy", "0", "0"], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (output / "demo/r1/03_alignment/alignment.json").exists()
