from __future__ import annotations

import os
import subprocess

import pytest

from cadscene.sfm import backend_detection
from cadscene.sfm.backend_detection import find_colmap_executable, select_sfm_backend


def _environment(**overrides):
    payload = {
        "pycolmap_available": True,
        "pycolmap_version": "4.1.0",
        "pycolmap_cuda_available": False,
        "colmap_cli_available": False,
        "colmap_path": None,
        "colmap_version": None,
        "colmap_cuda_confirmed": False,
        "cuda_device_available": True,
        "gpu_names": ["NVIDIA RTX"],
    }
    payload.update(overrides)
    return payload


def test_auto_prefers_confirmed_colmap_cli_cuda() -> None:
    selection = select_sfm_backend(
        _environment(
            colmap_cli_available=True,
            colmap_path="COLMAP.bat",
            colmap_version="3.13.0 CUDA enabled",
            colmap_cuda_confirmed=True,
            pycolmap_cuda_available=True,
        ),
        backend="auto",
        device="auto",
        gpu_index="0",
    )

    assert selection.backend == "colmap_cli"
    assert selection.effective_device == "cuda"
    assert selection.cpu_fallback is False


def test_unverified_colmap_gpu_is_not_reported_as_cuda() -> None:
    selection = select_sfm_backend(
        _environment(
            colmap_cli_available=True,
            colmap_path="colmap.exe",
            colmap_cuda_confirmed=False,
        ),
        backend="colmap_cli",
        device="cuda",
        gpu_index="0",
        allow_cpu_fallback=True,
    )

    assert selection.effective_device == "cpu"
    assert selection.cpu_fallback is True
    assert any("CUDA" in warning for warning in selection.warnings)


def test_no_cpu_fallback_rejects_unavailable_cuda() -> None:
    with pytest.raises(RuntimeError, match="CUDA"):
        select_sfm_backend(
            _environment(),
            backend="pycolmap",
            device="cuda",
            gpu_index="0",
            allow_cpu_fallback=False,
        )


def test_auto_falls_back_to_pycolmap_cpu_with_warning() -> None:
    selection = select_sfm_backend(
        _environment(cuda_device_available=False, gpu_names=[]),
        backend="auto",
        device="auto",
        gpu_index="0",
    )

    assert selection.backend == "pycolmap"
    assert selection.effective_device == "cpu"
    assert selection.cpu_fallback is True
    assert selection.warnings


def test_find_colmap_executable_discovers_project_local_install(tmp_path) -> None:
    executable = tmp_path / ".local" / "colmap" / "colmap-x64-windows-cuda" / "COLMAP.bat"
    executable.parent.mkdir(parents=True)
    executable.write_text("@echo off\n", encoding="ascii")

    found = find_colmap_executable(project_root=tmp_path)

    assert found == executable.resolve()


def test_find_colmap_executable_discovers_owner_install_from_worktree(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    worktree = repository / ".worktrees" / "srt-pose"
    worktree.mkdir(parents=True)
    executable = (
        repository / ".local" / "colmap" / "4.1.0-cuda" / "bin" / "colmap.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    monkeypatch.delenv("COLMAP_EXE", raising=False)
    monkeypatch.setattr(backend_detection.shutil, "which", lambda _name: None)

    found = find_colmap_executable(project_root=worktree)

    assert found == executable.resolve()


def test_environment_probe_hides_windows_console(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(backend_detection.subprocess, "run", fake_run)

    backend_detection._run_probe(["colmap.exe", "--version"])

    if os.name == "nt":
        assert calls[0][1]["creationflags"] & subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in calls[0][1]
