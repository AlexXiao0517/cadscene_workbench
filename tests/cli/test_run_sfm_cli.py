from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from cadscene.cli.run_sfm import build_parser


def test_default_backend_restores_legacy_pycolmap_cpu() -> None:
    args = build_parser().parse_args(["--dataset", "demo", "--run-id", "default"])

    assert args.backend == "pycolmap"
    assert args.device == "cpu"


def test_help_runs_without_pycolmap() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.run_sfm", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--no-mask" in result.stdout


def test_missing_video_has_clear_error(tmp_path: Path) -> None:
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_sfm",
            "--dataset",
            "demo",
            "--run-id",
            "missing",
            "--output-root",
            str(output_root),
            "--video",
            str(tmp_path / "missing.mp4"),
            "--no-mask",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "video does not exist" in result.stderr
    job_status = json.loads(
        (output_root / "demo" / "missing" / "job_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert job_status["status"] == "failed"
    assert job_status["stages"]["sfm"]["status"] == "failed"


def test_workflow_cli_forces_utf8_output_even_when_parent_requests_gbk(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "gbk"
    environment["CADSCENE_WORKFLOW_LOG_ENCODING"] = "utf-8"
    missing_video = tmp_path / "测试视频.mp4"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_sfm",
            "--dataset",
            "demo",
            "--run-id",
            "utf8-log",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(missing_video),
            "--no-mask",
        ],
        capture_output=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "测试视频.mp4" in result.stderr.decode("utf-8")


def test_mock_export_generates_outputs_and_manifest(tmp_path: Path) -> None:
    fixture = tmp_path / "mock_reconstruction.json"
    fixture.write_text(
        json.dumps(
            {
                "fps": 25.0,
                "width": 64,
                "height": 48,
                "intrinsics": [{"model": "OPENCV", "width": 64, "height": 48, "params": [50, 50, 32, 24]}],
                "poses": [
                    {
                        "frame_index": 0,
                        "registered": True,
                        "center": [0, 0, 0],
                        "cam_from_world_quat_wxyz": [1, 0, 0, 0],
                    },
                    {
                        "frame_index": 5,
                        "registered": True,
                        "center": [1, 0, 0],
                        "cam_from_world_quat_wxyz": [1, 0, 0, 0],
                    },
                ],
                "points": [[0, 0, 0, 255, 0, 0], [1, 0, 0, 0, 255, 0]],
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_sfm",
            "--dataset",
            "demo",
            "--run-id",
            "mock",
            "--output-root",
            str(output_root),
            "--export-only",
            "--mock-reconstruction",
            str(fixture),
            "--no-mask",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    stage_dir = output_root / "demo" / "mock" / "02_sfm"
    for name in (
        "camera_trajectory.json",
        "sparse_points.ply",
        "camera_intrinsics.json",
        "sfm_stats.json",
        "sfm_report.md",
    ):
        assert (stage_dir / name).exists()
    manifest = json.loads((output_root / "demo" / "mock" / "manifest.json").read_text(encoding="utf-8"))
    assert any(stage["stage_name"] == "sfm" for stage in manifest["stages"])
    job_status = json.loads(
        (output_root / "demo" / "mock" / "job_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert job_status["status"] == "success"
    assert job_status["stages"]["sfm"]["status"] == "success"
    assert job_status["stages"]["sfm"]["progress"] == 1.0
