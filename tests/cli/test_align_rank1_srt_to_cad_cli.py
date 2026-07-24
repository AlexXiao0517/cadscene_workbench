from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

import pytest

from cadscene.cli.align_rank1_srt_to_cad import _load_srt_samples, main


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
        writer = csv.DictWriter(stream, fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "rel_alt", "abs_alt", "gps_valid", "height_valid"])
        writer.writeheader()
        for index, frame in enumerate(frames):
            east = 4.0 * np.sin(index / 3.0) if rank2 else 0.0
            writer.writerow({"frame_index": frame, "frame_time_sec": frame / 30.0, "east_m": east, "north_m": 2.0 * index, "up_m": 0.0, "rel_alt": 50.0, "abs_alt": 120.0, "gps_valid": "true", "height_valid": "true"})

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
    assert alignment["quality"]["along_translation_spread_m"] == 0.0
    assert alignment["quality"]["lateral_translation_spread_m"] == 0.0
    assert alignment["quality"]["solve_height_offset_range_m"] == 0.0
    assert alignment["srt_constraint"] == "along_track_and_relative_height"
    trajectory = json.loads((output / "camera_trajectory_rank1_cad.json").read_text(encoding="utf-8"))
    assert trajectory["meta"]["vertical_source"] == "srt_relative_altitude_primary"
    assert trajectory["meta"]["height_datum"] == "manual_solve_anchors"
    assert trajectory["meta"]["absolute_elevation_available"] is False
    assert trajectory["meta"]["orientation_source"] == "sfm_plus_manual_prior"
    for field in (
        "srt_height_valid",
        "srt_relative_height_m",
        "vertical_correction_m",
        "vertical_smoothing_support",
        "sfm_vertical_detail_m",
    ):
        assert field in trajectory["poses"][0]
    stats = json.loads((output / "rank1_alignment_stats.json").read_text(encoding="utf-8"))
    assert stats["vertical_height_source"] == "rel_alt_relative"
    assert stats["vertical_height_coverage_ratio"] == 1.0
    assert stats["vertical_smoothing_window_sec"] == 2.0
    with (output / "camera_path_rank1_cad.csv").open(encoding="utf-8-sig", newline="") as stream:
        camera_fields = csv.DictReader(stream).fieldnames
    assert {
        "srt_height_valid",
        "srt_relative_height_m",
        "vertical_correction_m",
        "vertical_smoothing_support",
        "sfm_vertical_detail_m",
    }.issubset(set(camera_fields or ()))


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


def test_srt_samples_prefer_relative_altitude_and_rebase_it(tmp_path):
    path = tmp_path / "samples.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "rel_alt", "abs_alt", "gps_valid", "height_valid"],
        )
        writer.writeheader()
        writer.writerow({"frame_index": 0, "frame_time_sec": 0.0, "east_m": 0, "north_m": 0, "up_m": 0, "rel_alt": 50.0, "abs_alt": 120.0, "gps_valid": True, "height_valid": True})
        writer.writerow({"frame_index": 10, "frame_time_sec": 1.0, "east_m": 0, "north_m": 2, "up_m": 0, "rel_alt": 50.3, "abs_alt": 140.0, "gps_valid": True, "height_valid": True})

    samples = _load_srt_samples(path)

    assert [row.relative_height_m for row in samples] == pytest.approx([0.0, 0.3])
    assert {row.height_source for row in samples} == {"rel_alt_relative"}


def test_srt_samples_fall_back_to_relative_absolute_altitude(tmp_path):
    path = tmp_path / "samples.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "abs_alt", "gps_valid", "height_valid"],
        )
        writer.writeheader()
        writer.writerow({"frame_index": 0, "frame_time_sec": 0.0, "east_m": 0, "north_m": 0, "up_m": 0, "abs_alt": 120.0, "gps_valid": True, "height_valid": True})
        writer.writerow({"frame_index": 10, "frame_time_sec": 1.0, "east_m": 0, "north_m": 2, "up_m": 0, "abs_alt": 120.4, "gps_valid": True, "height_valid": True})

    samples = _load_srt_samples(path)

    assert [row.relative_height_m for row in samples] == pytest.approx([0.0, 0.4])
    assert {row.height_source for row in samples} == {"abs_alt_relative"}


def test_srt_samples_reject_missing_relative_and_absolute_height(tmp_path):
    path = tmp_path / "samples.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["frame_index", "frame_time_sec", "east_m", "north_m", "up_m", "gps_valid", "height_valid"],
        )
        writer.writeheader()
        writer.writerow({"frame_index": 0, "frame_time_sec": 0.0, "east_m": 0, "north_m": 0, "up_m": 0, "gps_valid": True, "height_valid": True})

    with pytest.raises(ValueError, match="rel_alt or abs_alt"):
        _load_srt_samples(path)


def test_vertical_height_coverage_counts_only_valid_height_samples(tmp_path):
    paths = _write_inputs(tmp_path)
    samples_path = paths[1]
    with samples_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fieldnames = list(rows[0])
    rows[5]["height_valid"] = "false"
    with samples_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    output = tmp_path / "03_rank1_alignment"

    assert main(_argv(paths, output)) == 0

    stats = json.loads((output / "rank1_alignment_stats.json").read_text(encoding="utf-8"))
    assert stats["vertical_height_coverage_ratio"] == pytest.approx(19 / 20)
