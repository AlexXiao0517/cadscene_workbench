from __future__ import annotations

import csv
import json

from cadscene.cli.align_rank1_srt_to_cad import main
from cadscene.sfm.trajectory import load_sfm_trajectory


def test_synthetic_rank1_cli_output_loads_as_standard_trajectory(tmp_path):
    frames = list(range(0, 160, 10))
    trajectory_path = tmp_path / "trajectory.json"
    trajectory_path.write_text(json.dumps({
        "fps": 25.0, "width": 1280, "height": 720,
        "intrinsics": [{"width": 1280, "height": 720, "params": [800.0]}],
        "poses": [{"frame_index": frame, "registered": True, "center": [index, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]} for index, frame in enumerate(frames)],
    }), encoding="utf-8")
    samples_path = tmp_path / "samples.csv"
    with samples_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "rel_alt", "gps_valid", "height_valid"])
        writer.writeheader()
        for index, frame in enumerate(frames):
            writer.writerow({"frame_index": frame, "frame_time_sec": frame / 25.0, "east_m": 0.0, "north_m": 3.0 * index, "up_m": 0.0, "rel_alt": 40.0, "gps_valid": True, "height_valid": True})
    def anchor(index: int, role: str) -> dict:
        return {"frame": frames[index], "source_frame_index": frames[index], "time": frames[index] / 25.0, "pts_time_sec": frames[index] / 25.0, "source": "manual_anchor", "camera": {"x": 10.0, "y": -2.0 + 3.0 * index, "z": 4.0, "yaw": -90.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0}, "orientation_metadata": {"orientation_source": "manual", "orientation_confirmed": True, "yaw_confirmed": True, "pitch_confirmed": True, "roll_confirmed": False}, "prior": {"enabled": True, "direction_type": "camera_forward", "solver_role": role, "quality": "confirmed"}}
    track_path = tmp_path / "track.json"
    track_path.write_text(json.dumps({"schema_version": 2, "coordinate_system": "web_cad_world", "pose_convention": {}, "keyframes": [anchor(0, "solve"), anchor(15, "validate")]}), encoding="utf-8")
    output = tmp_path / "03_rank1_alignment"

    code = main(["--trajectory", str(trajectory_path), "--srt-samples", str(samples_path), "--camera-track", str(track_path), "--output-dir", str(output), "--min-baseline-m", "2", "--min-smoothing-support", "1"])

    assert code == 0
    loaded = load_sfm_trajectory(output / "camera_trajectory_rank1_cad.json")
    assert len(loaded.frames) == len(frames)
    assert loaded.centers[0].tolist() == [10.0, -2.0, 4.0]
    assert loaded.centers[-1].tolist() == [10.0, 43.0, 4.0]
