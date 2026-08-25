from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from scripts.windows_offline_bundle import (
    BundleLayout,
    backend_runtime_files,
    validate_staging_tree,
    write_zip64,
)


def test_bundle_layout_keeps_all_writable_state_below_bundle_root(tmp_path: Path) -> None:
    layout = BundleLayout(tmp_path / "CADSceneWorkbench-0.1.0-win64-offline")

    assert layout.runtime == layout.root / "runtime"
    assert layout.backend == layout.root / "pure_rotation_backend"
    assert layout.workspace == layout.root / "workspace"
    assert layout.logs == layout.root / "logs"
    assert layout.launcher == layout.root / "launcher"


@pytest.mark.parametrize(
    "relative_path",
    (
        ".git/config",
        "projects/project-1/project_manifest.json",
        "data/dataset/video.mp4",
        "runs/run-1/job.json",
        "tests/test_runtime.py",
        "service.log",
        "logs/old-service.log",
    ),
)
def test_validate_staging_tree_rejects_development_or_user_content(
    tmp_path: Path,
    relative_path: str,
) -> None:
    root = tmp_path / "bundle"
    forbidden = root / relative_path
    forbidden.parent.mkdir(parents=True, exist_ok=True)
    forbidden.write_text("must not ship", encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden bundle content"):
        validate_staging_tree(root)


def test_empty_runtime_workspace_and_log_directories_are_allowed(tmp_path: Path) -> None:
    layout = BundleLayout(tmp_path / "bundle")
    for directory in (
        layout.runtime,
        layout.backend,
        layout.workspace,
        layout.logs,
        layout.launcher,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    validate_staging_tree(layout.root)


def test_backend_runtime_files_are_an_explicit_allowlist(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    wanted = (
        Path("src/pair_estimation.py"),
        Path("scripts/run_full_video_exploration.py"),
        Path("outputs/build_opengv_cli/opengv_rotation_cli.exe"),
        Path("backend_version.json"),
    )
    unwanted = (
        Path("tests/test_real_video.py"),
        Path("outputs/real-video/summary.json"),
        Path("reports/full_report.md"),
        Path(".git/config"),
    )
    for relative in (*wanted, *unwanted):
        path = backend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    selected = set(backend_runtime_files(backend))

    assert set(wanted) <= selected
    assert not (set(unwanted) & selected)


def test_write_zip64_uses_one_top_level_bundle_directory(tmp_path: Path) -> None:
    root = tmp_path / "CADSceneWorkbench-0.1.0-win64-offline"
    (root / "launcher").mkdir(parents=True)
    (root / "launcher" / "release.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "bundle.zip"

    write_zip64(root, output)

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "CADSceneWorkbench-0.1.0-win64-offline/launcher/release.json"
        ]
        assert archive._allowZip64 is True

