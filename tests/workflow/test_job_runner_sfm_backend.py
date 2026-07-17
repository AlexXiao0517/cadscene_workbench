from __future__ import annotations

from pathlib import Path

from cadscene.workflow.job_runner import build_stage_command


def test_sfm_job_defaults_to_legacy_pycolmap_cpu(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    command = build_stage_command(
        tmp_path,
        "demo",
        "default-run",
        "sfm",
        {"video": str(video)},
    )

    assert command[command.index("--backend") + 1] == "pycolmap"
    assert command[command.index("--device") + 1] == "cpu"


def test_sfm_job_command_passes_backend_device_and_gpu_index(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    command = build_stage_command(
        tmp_path,
        "demo",
        "gpu-run",
        "sfm",
        {
            "video": str(video),
            "backend": "colmap_cli",
            "device": "cuda",
            "gpu_index": "1",
            "colmap_exe": str(tmp_path / "COLMAP.bat"),
            "no_cpu_fallback": True,
        },
    )

    assert command[command.index("--backend") + 1] == "colmap_cli"
    assert command[command.index("--device") + 1] == "cuda"
    assert command[command.index("--gpu-index") + 1] == "1"
    assert command[command.index("--colmap-exe") + 1].endswith("COLMAP.bat")
    assert "--no-cpu-fallback" in command
