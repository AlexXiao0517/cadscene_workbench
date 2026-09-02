from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cadscene.cli import run_sfm as run_sfm_module
from cadscene.cli.run_sfm import build_parser


def test_default_backend_restores_legacy_pycolmap_cpu() -> None:
    args = build_parser().parse_args(["--dataset", "demo", "--run-id", "default"])

    assert args.backend == "pycolmap"
    assert args.device == "cpu"
    assert args.cleanup_workspace is False


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


def test_sfm_cli_publishes_authoritative_stage_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    progress_path = tmp_path / "adapter_progress.json"

    def fake_reconstruction(**kwargs):
        kwargs["progress_callback"](
            "feature_matching", 0.52, "正在进行顺序匹配"
        )
        return SimpleNamespace(stats={})

    monkeypatch.setattr(run_sfm_module, "run_reconstruction", fake_reconstruction)
    monkeypatch.setattr(
        run_sfm_module, "write_reconstruction_outputs", lambda *_args, **_kwargs: {}
    )

    result = run_sfm_module.main(
        [
            "--dataset",
            "demo",
            "--run-id",
            "progress",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(video),
            "--progress-file",
            str(progress_path),
        ]
    )

    assert result == 0
    assert json.loads(progress_path.read_text(encoding="utf-8")) == {
        "schema_version": "1.0",
        "stage": "feature_matching",
        "message": "正在进行顺序匹配",
        "fraction": 0.52,
    }


@pytest.mark.parametrize("cleanup", (False, True))
def test_sfm_cli_cleanup_is_opt_in_and_preserves_formal_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: bool,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    output_root = tmp_path / "runs"

    def fake_reconstruction(*, output_dir, **_kwargs):
        for relative in (
            "images/frame.jpg",
            "masks/frame.png",
            "database.db",
            "sparse/0/cameras.bin",
            "sparse_text_export/cameras.txt",
        ):
            path = output_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"scratch")
        return SimpleNamespace(stats={})

    def fake_write(stage_dir, *_args, **_kwargs):
        trajectory = stage_dir / "camera_trajectory.json"
        points = stage_dir / "sparse_points.ply"
        trajectory.write_text("{}", encoding="utf-8")
        points.write_text("ply", encoding="utf-8")
        return {"trajectory": trajectory, "sparse_points": points}

    monkeypatch.setattr(run_sfm_module, "run_reconstruction", fake_reconstruction)
    monkeypatch.setattr(run_sfm_module, "write_reconstruction_outputs", fake_write)
    argv = [
        "--dataset",
        "demo",
        "--run-id",
        "cleanup",
        "--output-root",
        str(output_root),
        "--video",
        str(video),
    ]
    if cleanup:
        argv.append("--cleanup-workspace")

    result = run_sfm_module.main(argv)

    assert result == 0
    stage = output_root / "demo/cleanup/02_sfm"
    assert (stage / "camera_trajectory.json").read_text("utf-8") == "{}"
    assert (stage / "sparse_points.ply").read_text("utf-8") == "ply"
    assert (stage / "database.db").exists() is (not cleanup)
    assert (stage / "images").exists() is (not cleanup)


def test_sfm_cleanup_failure_does_not_change_successful_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        run_sfm_module,
        "run_reconstruction",
        lambda **_kwargs: SimpleNamespace(stats={}),
    )
    monkeypatch.setattr(
        run_sfm_module,
        "write_reconstruction_outputs",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        run_sfm_module,
        "prune_sfm_workspace",
        lambda _stage: (_ for _ in ()).throw(OSError("cleanup denied")),
    )

    result = run_sfm_module.main(
        [
            "--dataset",
            "demo",
            "--run-id",
            "cleanup-error",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(video),
            "--cleanup-workspace",
        ]
    )

    assert result == 0
