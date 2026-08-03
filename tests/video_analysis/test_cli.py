from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import tomllib


def test_video_analysis_cli_exposes_isolated_inputs_without_workflow_execution() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.analyze_video", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--video" in result.stdout
    assert "--output-root" in result.stdout
    assert "--project-id" in result.stdout
    assert "--srt" in result.stdout
    assert "COLMAP" not in result.stdout
    assert "OpenGV" not in result.stdout


def test_export_video_clips_cli_help_exposes_required_paths() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.export_video_clips", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--manifest" in result.stdout
    assert "--output-dir" in result.stdout


def test_video_analysis_extra_declares_pts_and_visual_runtime_dependencies() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = pyproject["project"]["optional-dependencies"]["video_analysis"]

    assert any(item.startswith("imageio-ffmpeg") for item in dependencies)
    assert any(item.startswith("opencv-python") for item in dependencies)
