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
            "ba_global_frames_ratio": 3.5,
            "ba_global_points_ratio": 4.5,
            "ba_global_frames_freq": 321,
            "ba_global_points_freq": 654321,
            "ba_global_max_num_iterations": 17,
            "ba_global_max_refinements": 3,
        },
    )

    assert command[command.index("--backend") + 1] == "colmap_cli"
    assert command[command.index("--device") + 1] == "cuda"
    assert command[command.index("--gpu-index") + 1] == "1"
    assert command[command.index("--colmap-exe") + 1].endswith("COLMAP.bat")
    assert "--no-cpu-fallback" in command
    assert command[command.index("--ba-global-frames-ratio") + 1] == "3.5"
    assert command[command.index("--ba-global-points-ratio") + 1] == "4.5"
    assert command[command.index("--ba-global-frames-freq") + 1] == "321"
    assert command[command.index("--ba-global-points-freq") + 1] == "654321"
    assert command[command.index("--ba-global-max-num-iterations") + 1] == "17"
    assert command[command.index("--ba-global-max-refinements") + 1] == "3"
