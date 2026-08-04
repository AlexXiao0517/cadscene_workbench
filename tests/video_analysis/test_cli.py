from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import tomllib

import pytest

import cadscene.cli.export_video_clips as export_video_clips_cli
import cadscene.video_analysis.clip_export as clip_export


def test_video_analysis_cli_exposes_isolated_inputs_without_workflow_execution() -> (
    None
):
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


def test_export_video_clips_cli_help_exposes_all_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.export_video_clips", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    for option in (
        "--video",
        "--manifest",
        "--output-dir",
        "--ffmpeg",
        "--preset",
        "--crf",
    ):
        assert option in result.stdout


def test_export_video_clips_cli_requires_all_input_paths() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.export_video_clips"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2


@pytest.mark.parametrize(
    "option, value",
    [("--preset", "not-a-preset"), ("--crf", "not-an-integer")],
)
def test_export_video_clips_cli_rejects_invalid_encoding_options(
    option: str, value: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.export_video_clips",
            "--video",
            "source.mp4",
            "--manifest",
            "manifest.json",
            "--output-dir",
            "clips",
            option,
            value,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2


def test_export_video_clips_cli_uses_public_preset_choices() -> None:
    parser = export_video_clips_cli.build_parser()
    preset_action = next(
        action for action in parser._actions if action.dest == "preset"
    )

    assert preset_action.choices is clip_export.X264_PRESETS


def test_export_video_clips_cli_forwards_typed_default_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received: dict[str, object] = {}

    def export(
        video_path: Path,
        manifest_path: Path,
        output_dir: Path,
        *,
        ffmpeg_executable: str | Path | None = None,
        preset: str = "fast",
        crf: int = 18,
        require_full_source_partition: bool = True,
    ) -> list[Path]:
        received.update(
            video_path=video_path,
            manifest_path=manifest_path,
            output_dir=output_dir,
            ffmpeg_executable=ffmpeg_executable,
            preset=preset,
            crf=crf,
            require_full_source_partition=require_full_source_partition,
        )
        return [output_dir / "clip-0001.mp4"]

    monkeypatch.setattr(export_video_clips_cli, "export_video_clips", export)
    output_dir = Path("clips")

    result = export_video_clips_cli.main(
        [
            "--video",
            "source.mp4",
            "--manifest",
            "manifest.json",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert result == 0
    assert received == {
        "video_path": Path("source.mp4"),
        "manifest_path": Path("manifest.json"),
        "output_dir": output_dir,
        "ffmpeg_executable": None,
        "preset": "fast",
        "crf": 18,
        "require_full_source_partition": True,
    }
    assert capsys.readouterr().out.splitlines() == [
        str(output_dir.resolve()),
        "Exported 1 clips",
    ]


def test_export_video_clips_cli_forwards_typed_explicit_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    received: dict[str, object] = {}

    def export(
        video_path: Path,
        manifest_path: Path,
        output_dir: Path,
        *,
        ffmpeg_executable: str | Path | None = None,
        preset: str = "fast",
        crf: int = 18,
        require_full_source_partition: bool = True,
    ) -> list[Path]:
        received.update(
            video_path=video_path,
            manifest_path=manifest_path,
            output_dir=output_dir,
            ffmpeg_executable=ffmpeg_executable,
            preset=preset,
            crf=crf,
            require_full_source_partition=require_full_source_partition,
        )
        return [output_dir / "clip-0001.mp4", output_dir / "clip-0002.mp4"]

    monkeypatch.setattr(export_video_clips_cli, "export_video_clips", export)
    output_dir = Path("clips")

    result = export_video_clips_cli.main(
        [
            "--video",
            "source.mp4",
            "--manifest",
            "manifest.json",
            "--output-dir",
            str(output_dir),
            "--ffmpeg",
            "ffmpeg-custom.exe",
            "--preset",
            "veryfast",
            "--crf",
            "20",
            "--allow-subset",
        ]
    )

    assert result == 0
    assert received == {
        "video_path": Path("source.mp4"),
        "manifest_path": Path("manifest.json"),
        "output_dir": output_dir,
        "ffmpeg_executable": Path("ffmpeg-custom.exe"),
        "preset": "veryfast",
        "crf": 20,
        "require_full_source_partition": False,
    }
    assert capsys.readouterr().out.splitlines() == [
        str(output_dir.resolve()),
        "Exported 2 clips",
    ]


def test_video_analysis_extra_declares_pts_and_visual_runtime_dependencies() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    dependencies = pyproject["project"]["optional-dependencies"]["video_analysis"]

    assert any(item.startswith("imageio-ffmpeg") for item in dependencies)
    assert any(item.startswith("opencv-python") for item in dependencies)
