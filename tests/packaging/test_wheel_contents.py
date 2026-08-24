from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]


def _build_python() -> str:
    candidates = [
        Path(os.environ.get("CADSCENE_WHEEL_BUILD_PYTHON", sys.executable)),
        Path(sys.executable),
        Path(sys.executable).parent / "envs" / "pure_rotation_poc" / "python.exe",
        Path(sys.executable).parent.parent / "envs" / "pure_rotation_poc" / "python.exe",
    ]
    for candidate in dict.fromkeys(path.resolve() for path in candidates):
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import setuptools, wheel"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return str(candidate)
    raise AssertionError("no Python environment can import setuptools and wheel")


def _build_wheel(destination: Path) -> Path:
    completed = subprocess.run(
        [
            _build_python(),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            str(destination),
            str(ROOT),
        ],
        cwd=destination,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")
    wheels = tuple(destination.glob("cadscene_workbench-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_official_applications_and_pinned_configs(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path)

    with ZipFile(wheel) as archive:
        members = set(archive.namelist())

    for required in (
        "apps/workflow_portal/index.html",
        "apps/project_workspace/index.html",
        "apps/project_library/index.html",
        "apps/project_library/project_library.js",
        "apps/web_camera_viewer/index.html",
        "apps/web_camera_viewer/vendor/three.min.js",
        "configs/pipelines/sfm_overlay_existing_sfm.yaml",
        "configs/pure_rotation/adapter-calibration/cameras.txt",
    ):
        assert required in members
    assert not any("web_camera_viewer_broken_stage3f" in item for item in members)
