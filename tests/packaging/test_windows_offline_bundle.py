from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tarfile
import zipfile

import pytest

from scripts.windows_offline_bundle import (
    BundleLayout,
    ReleaseConfig,
    assemble_bundle,
    backend_runtime_files,
    prepare_runtime_commands,
    verify_bundle,
    validate_staging_tree,
    write_zip64,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_PACKAGING = REPOSITORY_ROOT / "packaging" / "windows"


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
        Path("outputs/toolchains/llvm-mingw-20260616-ucrt-x86_64/bin/libc++.dll"),
        Path("outputs/toolchains/llvm-mingw-20260616-ucrt-x86_64/bin/libunwind.dll"),
        Path("backend_version.json"),
    )
    unwanted = (
        Path("tests/test_real_video.py"),
        Path("outputs/real-video/summary.json"),
        Path("reports/full_report.md"),
        Path("outputs/toolchains/llvm-mingw-20260616-ucrt-x86_64/bin/libLLVM-22.dll"),
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


def test_cmd_entry_points_are_bundle_relative_and_keep_errors_visible() -> None:
    start = (WINDOWS_PACKAGING / "启动CAD视频工作台.cmd").read_text(encoding="utf-8")
    stop = (WINDOWS_PACKAGING / "关闭CAD视频工作台.cmd").read_text(encoding="utf-8")

    assert "%~dp0launcher\\start.ps1" in start
    assert "%~dp0launcher\\stop.ps1" in stop
    assert "-ExecutionPolicy Bypass" in start
    assert "if errorlevel 1 pause" in start
    assert "if errorlevel 1 pause" in stop


def test_start_launcher_uses_only_bundle_local_runtime_and_storage() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert 'Join-Path $bundleRoot "runtime\\python.exe"' in source
    assert 'Join-Path $bundleRoot "workspace"' in source
    assert 'Join-Path $bundleRoot "pure_rotation_backend"' in source
    assert 'Join-Path $bundleRoot "logs"' in source
    assert "conda-unpack.exe" in source
    assert ".cadscene-relocated" in source
    assert '"doctor"' in source
    assert '"serve"' in source
    assert '"--bind", "127.0.0.1"' in source
    assert '"--storage-root", $workspace' in source
    assert '"--pure-rotation-backend-root", $backend' in source
    assert '"--pure-rotation-python", $python' in source
    assert "/apps/project_library/" in source
    assert "service-state.json" in source


def test_start_launcher_forces_utf8_and_bundle_working_directory_before_doctor() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in source
    assert '$env:PYTHONIOENCODING = "utf-8"' in source
    assert "Set-Location -LiteralPath $bundleRoot" in source
    assert source.index("Set-Location -LiteralPath $bundleRoot") < source.index("$doctorArguments")


def test_stop_launcher_validates_process_identity_before_stopping() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "stop.ps1").read_text(encoding="utf-8")

    assert "Get-CimInstance" in source
    assert "Get-Process" in source
    assert "ExecutablePath" in source
    assert "process_start_time" in source
    assert "cadscene.cli.main" in source
    assert "--storage-root" in source
    assert "Stop-Process" in source
    assert source.index("ExecutablePath") < source.index("Stop-Process")


def test_start_launcher_persists_process_start_time_for_pid_reuse_protection() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert "Get-Process" in source
    assert "process_start_time" in source
    assert "ToUniversalTime().ToString(\"o\")" in source


@pytest.mark.parametrize("script_name", ("start.ps1", "stop.ps1"))
def test_powershell_launchers_parse_without_errors(script_name: str) -> None:
    script = WINDOWS_PACKAGING / "launcher" / script_name
    command = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$tokens, [ref]$errors) > $null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_release_config_pins_bundle_and_backend_versions() -> None:
    config = ReleaseConfig.load(WINDOWS_PACKAGING / "release-config.json")

    assert config.bundle_name == "CADSceneWorkbench-0.1.0-win64-offline"
    assert config.application_version == "0.1.0"
    assert config.poc_commit == "85ab6404bfb5a07da8cdaaba0a1e5c4da10dc250"
    assert config.opengv_commit == "91f4b19c73450833a40e463ad3648aae80b3a7f3"


def test_prepare_runtime_commands_clone_install_check_and_pack(tmp_path: Path) -> None:
    commands = prepare_runtime_commands(
        conda=Path("C:/Miniforge/Scripts/conda.exe"),
        conda_pack=Path("C:/Miniforge/Scripts/conda-pack.exe"),
        source_prefix=Path("C:/envs/pure_rotation"),
        build_prefix=tmp_path / "build env",
        wheel=tmp_path / "cadscene_workbench-0.1.0-py3-none-any.whl",
        runtime_archive=tmp_path / "runtime.tar.gz",
    )

    assert commands[0][:4] == (
        "C:\\Miniforge\\Scripts\\conda.exe",
        "create",
        "--yes",
        "--prefix",
    )
    assert "--clone" in commands[0]
    assert commands[1][1:4] == ("-m", "pip", "install")
    assert commands[2][1:] == ("-m", "pip", "check")
    assert commands[3][1:3] == ("-c", "import av, cv2, numpy, scipy, yaml, PIL, ezdxf, imageio_ffmpeg, pycolmap")
    assert commands[4][1:] == ("-m", "pip", "install", "conda-pack==0.9.2")
    assert commands[5][0].endswith("build env\\Scripts\\conda-pack.exe")
    assert "--force" in commands[5]


def _write_fake_runtime_archive(path: Path) -> None:
    source = path.parent / "fake-runtime"
    (source / "Scripts").mkdir(parents=True)
    (source / "python.exe").write_bytes(b"python")
    (source / "Scripts" / "conda-unpack.exe").write_bytes(b"unpack")
    with tarfile.open(path, "w:gz") as archive:
        for item in sorted(source.rglob("*")):
            archive.add(item, arcname=item.relative_to(source).as_posix())


def test_assemble_bundle_uses_runtime_archive_and_pruned_backend(tmp_path: Path) -> None:
    config = ReleaseConfig.load(WINDOWS_PACKAGING / "release-config.json")
    archive = tmp_path / "runtime.tar.gz"
    _write_fake_runtime_archive(archive)
    backend = tmp_path / "backend"
    for relative in (
        Path("src/pair_estimation.py"),
        Path("scripts/run_full_video_exploration.py"),
        Path("outputs/build_opengv_cli/opengv_rotation_cli.exe"),
        Path("outputs/toolchains/llvm-mingw-20260616-ucrt-x86_64/bin/libc++.dll"),
        Path("outputs/toolchains/llvm-mingw-20260616-ucrt-x86_64/bin/libunwind.dll"),
        Path("outputs/real-video/private-result.json"),
        Path("tests/test_backend.py"),
    ):
        item = backend / relative
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("x", encoding="utf-8")
    output = tmp_path / "dist"

    bundle = assemble_bundle(
        config=config,
        runtime_archive=archive,
        backend_root=backend,
        templates_root=WINDOWS_PACKAGING,
        output_dir=output,
        source_commit="52c605f",
    )

    assert (bundle / "runtime" / "python.exe").is_file()
    assert (bundle / "启动CAD视频工作台.cmd").is_file()
    assert (bundle / "pure_rotation_backend" / "src" / "pair_estimation.py").is_file()
    assert (
        bundle
        / "pure_rotation_backend"
        / "outputs"
        / "toolchains"
        / "llvm-mingw-20260616-ucrt-x86_64"
        / "bin"
        / "libc++.dll"
    ).is_file()
    assert (
        bundle
        / "pure_rotation_backend"
        / "outputs"
        / "toolchains"
        / "llvm-mingw-20260616-ucrt-x86_64"
        / "bin"
        / "libunwind.dll"
    ).is_file()
    assert not (bundle / "pure_rotation_backend" / "tests").exists()
    assert not (bundle / "pure_rotation_backend" / "outputs" / "real-video").exists()
    backend_version = json.loads(
        (bundle / "pure_rotation_backend" / "backend_version.json").read_text(encoding="utf-8")
    )
    assert backend_version == {
        "poc_commit": config.poc_commit,
        "opengv_commit": config.opengv_commit,
    }
    release = json.loads((bundle / "launcher" / "release.json").read_text(encoding="utf-8"))
    assert release["source_commit"] == "52c605f"
    assert any(path.endswith("/libc++.dll") for path in release["critical_sha256"])
    assert any(path.endswith("/libunwind.dll") for path in release["critical_sha256"])
    verify_bundle(bundle, config=config)


def test_verify_bundle_rejects_missing_runtime_tool(tmp_path: Path) -> None:
    config = ReleaseConfig.load(WINDOWS_PACKAGING / "release-config.json")
    layout = BundleLayout(tmp_path / config.bundle_name)
    layout.runtime.mkdir(parents=True)
    (layout.runtime / "python.exe").write_bytes(b"python")

    with pytest.raises(ValueError, match="conda-unpack.exe"):
        verify_bundle(layout.root, config=config)


def test_builder_cli_lists_prepare_assemble_and_verify_commands() -> None:
    completed = subprocess.run(
        ["python", str(REPOSITORY_ROOT / "scripts" / "windows_offline_bundle.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "prepare-runtime" in completed.stdout
    assert "assemble" in completed.stdout
    assert "verify" in completed.stdout
