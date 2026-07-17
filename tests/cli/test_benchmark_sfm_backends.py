from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cadscene.cli.benchmark_sfm_backends import assess_reconstruction_quality


def test_benchmark_help_runs_without_colmap() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.benchmark_sfm_backends", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--dry-run" in result.stdout


def test_benchmark_dry_run_writes_json_and_chinese_report(tmp_path: Path) -> None:
    video = tmp_path / "tiny.mp4"
    video.write_bytes(b"placeholder")
    report_dir = tmp_path / "reports"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.benchmark_sfm_backends",
            "--dataset",
            "demo",
            "--run-id-prefix",
            "bench",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(video),
            "--report-dir",
            str(report_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((report_dir / "sfm_backend_benchmark.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in payload["backends"]] == [
        "pycolmap",
        "colmap_cli_cpu",
        "colmap_cli_cuda",
    ]
    assert (report_dir / "sfm_backend_benchmark.md").exists()


def test_benchmark_flags_clear_quality_regression() -> None:
    rows = [
        {
            "name": "pycolmap",
            "status": "success",
            "registered_ratio": 1.0,
            "point_count": 1000,
            "mean_reprojection_error": 0.5,
        },
        {
            "name": "colmap_cli_cuda",
            "status": "success",
            "registered_ratio": 0.5,
            "point_count": 300,
            "mean_reprojection_error": 2.0,
        },
    ]

    assessed = assess_reconstruction_quality(rows)

    assert assessed[0]["quality_comparison"] == "baseline"
    assert assessed[1]["quality_comparison"] == "degraded"
    assert assessed[1]["quality_warnings"]
