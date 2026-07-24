from __future__ import annotations

import json
import subprocess
import sys


def test_run_sfm_help_lists_backend_and_device_controls() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.run_sfm", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--backend" in result.stdout
    assert "--device" in result.stdout
    assert "--gpu-index" in result.stdout
    assert "--no-cpu-fallback" in result.stdout
    assert "--ba-global-frames-ratio" in result.stdout
    assert "--ba-global-points-ratio" in result.stdout
    assert "--ba-global-frames-freq" in result.stdout
    assert "--ba-global-points-freq" in result.stdout
    assert "--ba-global-max-num-iterations" in result.stdout
    assert "--ba-global-max-refinements" in result.stdout


def test_custom_ba_values_are_recorded_in_manifest_inputs(tmp_path) -> None:
    fixture = tmp_path / "mock_reconstruction.json"
    fixture.write_text(
        json.dumps(
            {
                "fps": 25.0,
                "width": 64,
                "height": 48,
                "intrinsics": [],
                "poses": [],
                "points": [],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"
    expected = {
        "ba_global_frames_ratio": 3.25,
        "ba_global_points_ratio": 4.5,
        "ba_global_frames_freq": 321,
        "ba_global_points_freq": 654321,
        "ba_global_max_num_iterations": 17,
        "ba_global_max_refinements": 3,
    }

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_sfm",
            "--dataset",
            "demo",
            "--run-id",
            "custom-ba",
            "--output-root",
            str(output_root),
            "--mock-reconstruction",
            str(fixture),
            "--ba-global-frames-ratio",
            str(expected["ba_global_frames_ratio"]),
            "--ba-global-points-ratio",
            str(expected["ba_global_points_ratio"]),
            "--ba-global-frames-freq",
            str(expected["ba_global_frames_freq"]),
            "--ba-global-points-freq",
            str(expected["ba_global_points_freq"]),
            "--ba-global-max-num-iterations",
            str(expected["ba_global_max_num_iterations"]),
            "--ba-global-max-refinements",
            str(expected["ba_global_max_refinements"]),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(
        (output_root / "demo" / "custom-ba" / "manifest.json").read_text(encoding="utf-8")
    )
    assert {name: manifest["stages"][-1]["inputs"][name] for name in expected} == expected
