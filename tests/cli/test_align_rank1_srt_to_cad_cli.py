from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from cadscene.cli.align_rank1_srt_to_cad import main


def _write_inputs(root: Path, *, include_validate: bool = True, rank2: bool = False) -> tuple[Path, Path, Path]:
    frames = list(range(0, 200, 10))
    trajectory = {
        "fps": 30.0, "width": 1920, "height": 1080,
        "intrinsics": [{"width": 1920, "height": 1080, "params": [1000.0]}],
        "poses": [{"frame_index": frame, "registered": True, "center": [index, 0.0, 0.0], "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0]} for index, frame in enumerate(frames)],
    }
    trajectory_path = root / "camera_trajectory.json"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")

    samples_path = root / "srt_frame_samples.csv"
    with samples_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "gps_valid", "height_valid"])
        writer.writeheader()
        for index, frame in enumerate(frames):
            east = 4.0 * np.sin(index / 3.0) if rank2 else 0.0
            writer.writerow({"frame_index": frame, "frame_time_sec": frame / 30.0, "east_m": east, "north_m": 2.0 * index, "up_m": 0.0, "gps_valid": "true", "height_valid": "true"})

    def keyframe(index: int, role: str) -> dict:
        return {
            "frame": frames[index], "source_frame_index": frames[index], "time": frames[index] / 30.0, "pts_time_sec": frames[index] / 30.0,
            "source": "manual_anchor",
            "camera": {"x": 5.0, "y": 7.0 + 2.0 * index, "z": 3.0, "yaw": -90.0, "pitch": 0.0, "roll": 0.0, "fov": 75.0},
            "orientation_metadata": {"orientation_source": "manual", "orientation_confirmed": True, "yaw_confirmed": True, "pitch_confirmed": True, "roll_confirmed": False, "projection_checked": True, "projection_residual_px": 0.5},
            "prior": {"enabled": True, "direction_type": "camera_forward", "solver_role": role, "quality": "confirmed"},
        }
    keyframes = [keyframe(0, "solve")]
    if include_validate:
        keyframes.append(keyframe(len(frames) - 1, "validate"))
    track_path = root / "camera_track_v2.json"
    track_path.write_text(json.dumps({"schema_version": 2, "coordinate_system": "web_cad_world", "pose_convention": {}, "keyframes": keyframes}), encoding="utf-8")
    return trajectory_path, samples_path, track_path


def _argv(paths: tuple[Path, Path, Path], output: Path) -> list[str]:
    trajectory, samples, track = paths
    return ["--trajectory", str(trajectory), "--srt-samples", str(samples), "--camera-track", str(track), "--output-dir", str(output), "--min-baseline-m", "2", "--min-smoothing-support", "1"]


def test_cli_writes_validated_rank1_artifacts_atomically(tmp_path):
    output = tmp_path / "03_rank1_alignment"
    assert main(_argv(_write_inputs(tmp_path), output)) == 0

    expected = {
        "rank1_alignment.json", "camera_trajectory_rank1_cad.json", "camera_path_rank1_cad.csv",
        "rank1_alignment_stats.json", "rank1_alignment_report.md", "rank1_validation.csv", "rank1_trajectory_comparison.csv",
    }
    assert expected.issubset({path.name for path in output.iterdir()})
    alignment = json.loads((output / "rank1_alignment.json").read_text(encoding="utf-8"))
    assert alignment["method"] == "rank1_srt_manual_orientation"
    assert alignment["coordinate_system"] == "cad_meters"
    assert alignment["scale"] == 2.0
    assert alignment["quality"]["holdout_accepted"] is True


def test_missing_validate_anchor_writes_failure_report_only(tmp_path):
    output = tmp_path / "03_rank1_alignment"
    assert main(_argv(_write_inputs(tmp_path, include_validate=False), output)) == 1
    assert (output / "rank1_failure_report.md").is_file()
    assert "holdout" in (output / "rank1_failure_report.md").read_text(encoding="utf-8").lower()
    assert not (output / "camera_trajectory_rank1_cad.json").exists()


def test_disabling_validate_requirement_still_cannot_create_formal_output(tmp_path):
    output = tmp_path / "03_rank1_alignment"
    argv = _argv(_write_inputs(tmp_path, include_validate=False), output) + ["--no-require-validate-anchor"]

    assert main(argv) == 1
    assert (output / "rank1_failure_report.md").is_file()
    assert not (output / "rank1_alignment.json").exists()
    assert not (output / "camera_trajectory_rank1_cad.json").exists()


def test_rank2_input_does_not_enter_rank1_solver(tmp_path):
    output = tmp_path / "03_rank1_alignment"
    assert main(_argv(_write_inputs(tmp_path, rank2=True), output)) == 1
    assert "rank=1" in (output / "rank1_failure_report.md").read_text(encoding="utf-8")
    assert not (output / "rank1_alignment.json").exists()
