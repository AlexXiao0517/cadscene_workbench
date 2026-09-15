from __future__ import annotations

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def _project_metadata() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def _dependency_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip().lower()


def test_distribution_and_runtime_versions_match_release_0_1_5() -> None:
    from cadscene import __version__

    project = _project_metadata()

    assert project["version"] == "0.1.5"
    assert __version__ == project["version"]


def test_distribution_declares_mit_and_preserves_third_party_notices() -> None:
    project = _project_metadata()
    assert project["license"] == "MIT"
    assert set(project["license-files"]) == {
        "LICENSE", "THIRD_PARTY_NOTICES.md", "third_party_licenses/*.txt"
    }
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 CADScene Workbench contributors" in license_text
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    third_party = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "FFmpeg" in third_party and "GPL" in third_party
    assert "Three.js" in third_party


def test_default_install_declares_supported_runtime_dependencies() -> None:
    project = _project_metadata()

    names = {
        _dependency_name(str(requirement))
        for requirement in project["dependencies"]  # type: ignore[index]
    }

    assert {
        "numpy",
        "scipy",
        "pyyaml",
        "pillow",
        "opencv-python",
        "ezdxf",
        "imageio-ffmpeg",
        "pycolmap",
        "pyproj",
        "psutil",
    } <= names


def test_semantic_segmentation_is_not_an_install_extra() -> None:
    project = _project_metadata()

    assert "segmentation" not in project.get("optional-dependencies", {})


def test_distribution_declares_the_unified_console_script() -> None:
    project = _project_metadata()

    assert project["scripts"] == {  # type: ignore[index]
        "cadscene-workbench": "cadscene.cli.main:main"
    }


def test_broken_stage3f_viewer_is_not_distributed_from_source() -> None:
    assert not (ROOT / "apps" / "web_camera_viewer_broken_stage3f").exists()


def test_development_install_declares_wheel_build_tooling() -> None:
    project = _project_metadata()
    dev = project["optional-dependencies"]["dev"]  # type: ignore[index]

    assert any(str(requirement).startswith("wheel") for requirement in dev)
