from __future__ import annotations

from pathlib import Path
import io
import json
import subprocess
import sys
import tarfile
import zipfile

import pytest
import scripts.windows_offline_bundle as offline_bundle

from scripts.windows_offline_bundle import (
    BundleLayout,
    ReleaseConfig,
    assemble_bundle,
    backend_runtime_files,
    prepare_runtime_commands,
    proj_runtime_smoke_code,
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


def test_validate_staging_tree_rejects_a_runtime_already_relocated_in_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bundle"
    marker = root / "runtime" / ".cadscene-relocated"
    marker.parent.mkdir(parents=True)
    marker.touch()

    with pytest.raises(ValueError, match="cadscene-relocated"):
        validate_staging_tree(root)


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


def test_backend_runtime_files_reuses_colocated_native_libraries(tmp_path: Path) -> None:
    backend = tmp_path / "released-backend"
    colocated = (
        Path("outputs/build_opengv_cli/libc++.dll"),
        Path("outputs/build_opengv_cli/libunwind.dll"),
    )
    for relative in colocated:
        path = backend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("released runtime", encoding="utf-8")

    selected = set(backend_runtime_files(backend))

    assert set(colocated) <= selected


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


def test_tester_instructions_explain_same_machine_workspace_upgrade() -> None:
    instructions = (WINDOWS_PACKAGING / "使用说明.txt").read_text(encoding="utf-8")

    assert "同一台电脑从旧版升级" in instructions
    assert "完整复制旧版的 workspace" in instructions
    assert "请勿删除旧版原位置自动建立的 workspace Junction" in instructions
    assert "workspace.pre-0.1.3-" in instructions


def test_start_launcher_uses_only_bundle_local_runtime_and_storage() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert 'Join-Path $bundleRoot "runtime\\python.exe"' in source
    assert 'Join-Path $bundleRoot "workspace"' in source
    assert 'Join-Path $bundleRoot "pure_rotation_backend"' in source
    assert 'Join-Path $bundleRoot "logs"' in source
    assert 'Join-Path $runtime "Scripts\\conda-unpack-script.py"' in source
    assert "& $python $unpackScript" in source
    assert "& $unpacker" not in source
    assert ".cadscene-relocated" in source
    assert '"doctor"' in source
    assert '"serve"' in source
    assert '"--bind", "127.0.0.1"' in source
    assert '"--storage-root", $workspace' in source
    assert '"--pure-rotation-backend-root", $backend' in source
    assert '"--pure-rotation-python", $python' in source
    assert "/apps/project_library/" in source
    assert "service-state.json" in source


def test_start_launcher_rejects_an_unsafe_windows_path_before_runtime_setup() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert "function Assert-PathBudget" in source
    assert '$probeProject = "dataset-00000000-0000-0000-0000-000000000000"' in source
    assert '".va-00000000\\r"' in source
    assert '"v\\r\\r-0000000000000000\\video_analysis_manifest.json"' in source
    assert '"02_video_analysis\\analysis_revisions\\r-0000000000000000\\video_analysis_manifest.json"' not in source
    assert '"analysis_artifacts\\.pub-00000000\\a\\02_video_analysis\\video_analysis_manifest.json"' in source
    assert '"analysis_artifacts\\va-0000000000000000\\02_video_analysis\\video_analysis_manifest.json"' in source
    assert "$probePaths" in source
    assert "$maxSafePathLength = 240" in source
    assert "Windows 路径过长" in source
    assert "请把包含启动文件的程序目录直接移动到较短位置，例如 D:\\CADScene" in source
    assert source.index("Assert-PathBudget") < source.index("conda-unpack-script.py")

    nested_release_workspace = Path(
        r"D:\zjic2026\cadscene_workbench\dist\releases\0.1.3\CADScene-0.1.3\workspace"
    )
    compact_probe = nested_release_workspace / (
        "projects/dataset-00000000-0000-0000-0000-000000000000/"
        "jobs/00000000000000000000000000000000/attempt-1/"
        "v/r/r-0000000000000000/"
        "video_analysis_manifest.json"
    )
    assert len(str(compact_probe)) <= 240


def test_start_launcher_forces_utf8_and_bundle_working_directory_before_doctor() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert '$env:PYTHONUTF8 = "1"' in source
    assert '$env:PYTHONIOENCODING = "utf-8"' in source
    assert "Set-Location -LiteralPath $bundleRoot" in source
    assert source.index("Set-Location -LiteralPath $bundleRoot") < source.index("$doctorArguments")


def test_start_launcher_repairs_opencv_paths_on_every_start_before_doctor() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    repair_command = '"-m", "cadscene.cli.runtime_relocation", "--runtime", $runtime'
    assert repair_command in source
    assert source.index(repair_command) < source.index("$doctorArguments")


def test_start_launcher_migrates_copied_workspace_with_a_recoverable_junction() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert "function Invoke-WorkspaceMigration" in source
    assert '"-m", "cadscene.cli.workspace_migration", "plan"' in source
    assert '"-m", "cadscene.cli.workspace_migration", "record"' in source
    assert "Get-CimInstance Win32_Process" in source
    assert "$candidate.CommandLine.Contains($oldWorkspace)" in source
    assert 'Split-Path -Leaf $oldWorkspace) -ine "workspace"' in source
    assert "Move-Item -LiteralPath $oldWorkspace -Destination $backupWorkspace" in source
    assert "New-Item -ItemType Junction -Path $oldWorkspace -Target $workspace" in source
    assert "Move-Item -LiteralPath $backupWorkspace -Destination $oldWorkspace" in source
    assert "旧版 CADScene 服务仍在使用" in source
    assert "创建兼容 Junction 失败" in source
    assert source.index("Invoke-WorkspaceMigration\n\n    $doctorArguments") > source.index(
        "conda-unpack-script.py"
    )


def test_start_launcher_recovers_an_interrupted_workspace_migration() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert '$migrationAction -eq "recover"' in source
    assert "$plan.displaced_workspace" in source
    assert '"projects\\.serve_viewer.lease"' in source
    assert "旧 workspace 空壳已恢复为兼容 Junction" in source


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


def test_stop_launcher_warns_before_interrupting_active_workbench_editing() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "stop.ps1").read_text(encoding="utf-8")

    assert "param([switch]$Force)" in source
    assert 'Join-Path $workspace "projects"' in source
    assert 'Join-Path $_.FullName "workbench_sessions"' in source
    assert '$session.state -eq "editing"' in source
    assert "expires_at" in source
    assert "Read-Host" in source
    assert "Stop-Process" in source


def test_small_hotfix_script_backs_up_files_and_repairs_opencv_without_workspace_writes() -> None:
    source = (WINDOWS_PACKAGING / "hotfix" / "apply-hotfix.ps1").read_text(
        encoding="utf-8"
    )

    assert 'Join-Path $bundleRoot "runtime\\python.exe"' in source
    assert 'Join-Path $bundleRoot "launcher\\start.ps1"' in source
    assert "hotfix-backup-" in source
    assert "Copy-Item -LiteralPath $sourceFile -Destination $destinationFile" in source
    assert '"-m", "cadscene.cli.runtime_relocation", "--runtime", $runtime' in source
    assert 'Join-Path $bundleRoot "workspace"' not in source
    assert "Remove-Item" not in source


def test_small_hotfix_cmd_runs_the_powershell_installer() -> None:
    source = (WINDOWS_PACKAGING / "hotfix" / "应用0.1.3修复.cmd").read_text(
        encoding="utf-8"
    )

    assert "%~dp0hotfix\\apply-hotfix.ps1" in source
    assert "if errorlevel 1 pause" in source


def test_start_launcher_persists_process_start_time_for_pid_reuse_protection() -> None:
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8")

    assert "Get-Process" in source
    assert "process_start_time" in source
    assert "ToUniversalTime().ToString(\"o\")" in source


@pytest.mark.parametrize(
    "script_name",
    ("start.ps1", "stop.ps1", "../hotfix/apply-hotfix.ps1"),
)
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

    assert config.bundle_name == "CADScene-0.1.5"
    assert config.application_version == "0.1.5"
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
    assert commands[3][1] == "-c"
    assert "import av, cv2, numpy, scipy, yaml, PIL, ezdxf" in commands[3][2]
    assert "import pyproj" in commands[3][2]
    assert "CRS.from_epsg(4549)" in commands[3][2]
    assert "Transformer.from_crs(4326, 4549" in commands[3][2]
    assert commands[4][1:] == ("-m", "pip", "install", "conda-pack==0.9.2")
    assert commands[5][0].endswith("build env\\python.exe")
    assert commands[5][1] == "-c"
    assert ".cadscene-relocated" in commands[5][2]
    assert "unlink(missing_ok=True)" in commands[5][2]
    assert commands[6][0].endswith("build env\\Scripts\\conda-pack.exe")
    assert "--force" in commands[6]


def test_proj_runtime_smoke_resolves_epsg4549_away_from_checkout(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    completed = subprocess.run(
        ["python", "-I", "-c", proj_runtime_smoke_code()],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def _write_fake_runtime_archive(path: Path) -> None:
    source = path.parent / "fake-runtime"
    (source / "Scripts").mkdir(parents=True, exist_ok=True)
    (source / "python.exe").write_bytes(b"python")
    (source / "Scripts" / "conda-unpack.exe").write_bytes(b"unpack")
    (source / "Scripts" / "conda-unpack-script.py").write_text(
        "_prefix_records = []\n", encoding="utf-8"
    )
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
    colmap = tmp_path / "colmap"
    (colmap / "bin").mkdir(parents=True)
    (colmap / "COLMAP.bat").write_text("@echo off")
    (colmap / "bin" / "colmap.exe").write_bytes(b"colmap")

    bundle = assemble_bundle(
        config=config,
        runtime_archive=archive,
        backend_root=backend,
        templates_root=WINDOWS_PACKAGING,
        output_dir=output,
        source_commit="52c605f",
        colmap_root=colmap,
        documentation_root=REPOSITORY_ROOT,
    )

    assert (bundle / "runtime" / "python.exe").is_file()
    assert (bundle / "LICENSE").read_bytes() == (REPOSITORY_ROOT / "LICENSE").read_bytes()
    assert (bundle / "THIRD_PARTY_NOTICES.md").is_file()
    assert (bundle / "third_party_licenses" / "threejs-MIT.txt").is_file()
    assert (bundle / "docs" / "technical" / "developer-guide.md").is_file()
    checklist = bundle / "packaging" / "windows" / "PUBLIC_RELEASE_CHECKLIST.md"
    assert checklist.is_file()
    assert (checklist.parent / "../../THIRD_PARTY_NOTICES.md").resolve().is_file()
    assert not (bundle / "PUBLIC_RELEASE_CHECKLIST.md").exists()
    assert not (bundle / "docs" / "SOP").exists()
    assert (bundle / "colmap" / "bin" / "colmap.exe").read_bytes() == b"colmap"
    assert (bundle / "启动CAD视频工作台.cmd").is_file()
    assert (bundle / "pure_rotation_backend" / "src" / "pair_estimation.py").is_file()
    assert (
        bundle
        / "pure_rotation_backend"
        / "outputs"
        / "build_opengv_cli"
        / "libc++.dll"
    ).is_file()
    assert (
        bundle
        / "pure_rotation_backend"
        / "outputs"
        / "build_opengv_cli"
        / "libunwind.dll"
    ).is_file()
    assert not (bundle / "pure_rotation_backend" / "outputs" / "toolchains").exists()
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


def test_launcher_selects_bundled_colmap_when_present():
    source = (WINDOWS_PACKAGING / "launcher" / "start.ps1").read_text(encoding="utf-8-sig")
    assert '"colmap\\COLMAP.bat"' in source
    assert '$env:COLMAP_EXE = $bundledColmap' in source
    assert 'Test-Path -LiteralPath $bundledColmap -PathType Leaf' in source


def test_verify_bundle_rejects_missing_runtime_tool(tmp_path: Path) -> None:
    config = ReleaseConfig.load(WINDOWS_PACKAGING / "release-config.json")
    layout = BundleLayout(tmp_path / config.bundle_name)
    layout.runtime.mkdir(parents=True)
    (layout.runtime / "python.exe").write_bytes(b"python")

    with pytest.raises(ValueError, match="conda-unpack-script.py"):
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


def _write_profile_runtime(runtime: Path, *, replacement: bytes, duplicate: bytes | None) -> None:
    replacement_path = runtime / "Library" / "bin" / "ffmpeg.exe"
    replacement_path.parent.mkdir(parents=True)
    replacement_path.write_bytes(replacement)
    scripts = runtime / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "conda-unpack-script.py").write_text(
        "_prefix_records = []\n", encoding="utf-8"
    )
    if duplicate is not None:
        duplicate_path = (
            runtime
            / "Lib"
            / "site-packages"
            / "imageio_ffmpeg"
            / "binaries"
            / "ffmpeg-win-x86_64-v7.1.exe"
        )
        duplicate_path.parent.mkdir(parents=True)
        duplicate_path.write_bytes(duplicate)


def test_conda_ffmpeg_profile_removes_only_audited_duplicate_and_writes_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"replacement", duplicate=b"duplicate")
    keep = runtime / "Lib" / "site-packages" / "imageio_ffmpeg" / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(runtime / "Library/bin/ffmpeg.exe"))
    duplicate_path = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", offline_bundle._sha256(duplicate_path))

    profile = offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    assert (runtime / "Library/bin/ffmpeg.exe").read_bytes() == b"replacement"
    assert not duplicate_path.exists()
    assert keep.read_text(encoding="utf-8") == "keep"
    assert profile == {
        "runtime_profile": "conda-ffmpeg-only",
        "replacement": {
            "path": "runtime/Library/bin/ffmpeg.exe",
            "sha256": offline_bundle._CONDA_FFMPEG_SHA256,
        },
        "excluded": {
            "path": "runtime/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
            "sha256": offline_bundle._IMAGEIO_FFMPEG_SHA256,
        },
        "redistribution_status": "pending",
        "conda_unpack_script": {
            "path": "runtime/Scripts/conda-unpack-script.py",
            "sha256_before": offline_bundle._sha256(runtime / "Scripts/conda-unpack-script.py"),
            "sha256_after": offline_bundle._sha256(runtime / "Scripts/conda-unpack-script.py"),
        },
    }


@pytest.mark.parametrize("duplicate", (b"duplicate", None))
def test_conda_ffmpeg_profile_requires_audited_replacement_without_removing_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplicate: bytes | None
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"wrong", duplicate=duplicate)
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", "0" * 64)
    duplicate_path = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"

    with pytest.raises(ValueError, match="replacement FFmpeg"):
        offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    if duplicate is not None:
        assert duplicate_path.read_bytes() == duplicate


def test_conda_ffmpeg_profile_rejects_unknown_duplicate_without_removing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"replacement", duplicate=b"unknown")
    replacement_path = runtime / "Library/bin/ffmpeg.exe"
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(replacement_path))
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", "0" * 64)
    duplicate_path = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"

    with pytest.raises(ValueError, match="imageio-ffmpeg duplicate"):
        offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    assert duplicate_path.read_bytes() == b"unknown"


def test_conda_ffmpeg_profile_rejects_missing_replacement_without_removing_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"temporary", duplicate=b"duplicate")
    (runtime / "Library/bin/ffmpeg.exe").unlink()
    duplicate_path = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", offline_bundle._sha256(duplicate_path))

    with pytest.raises(ValueError, match="replacement FFmpeg"):
        offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    assert duplicate_path.read_bytes() == b"duplicate"


def test_safe_runtime_extraction_preserves_default_in_tree_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "linked-runtime.tar"
    with tarfile.open(archive_path, "w") as archive:
        payload = tarfile.TarInfo("Library/bin/ffmpeg.exe")
        payload.size = len(b"ffmpeg")
        archive.addfile(payload, io.BytesIO(b"ffmpeg"))
        member = tarfile.TarInfo("Library/bin/ffmpeg-copy.exe")
        member.type = tarfile.LNKTYPE
        member.linkname = "Library/bin/ffmpeg.exe"
        archive.addfile(member)

    offline_bundle._safe_extract_tar(archive_path, tmp_path / "runtime")

    assert (tmp_path / "runtime/Library/bin/ffmpeg-copy.exe").read_bytes() == b"ffmpeg"


def test_profile_runtime_extraction_rejects_archive_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "linked-runtime.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("Library/bin/ffmpeg.exe")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside.exe"
        archive.addfile(member)

    with pytest.raises(ValueError, match="contains a link"):
        offline_bundle._safe_extract_tar(
            archive_path, tmp_path / "runtime", reject_links=True
        )


def test_conda_ffmpeg_profile_accepts_an_already_absent_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"replacement", duplicate=None)
    replacement = runtime / "Library/bin/ffmpeg.exe"
    monkeypatch.setattr(
        offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(replacement)
    )

    profile = offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    assert replacement.read_bytes() == b"replacement"
    assert profile["excluded"] is None


def test_assemble_conda_ffmpeg_profile_writes_runtime_trace_at_bundle_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "runtime-source"
    _write_profile_runtime(source, replacement=b"replacement", duplicate=b"duplicate")
    (source / "Scripts").mkdir(parents=True, exist_ok=True)
    (source / "python.exe").write_bytes(b"python")
    (source / "Scripts/conda-unpack-script.py").write_text(
        "_prefix_records = ['Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe']\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for item in sorted(source.rglob("*")):
            archive.add(item, arcname=item.relative_to(source).as_posix())
    replacement = source / "Library/bin/ffmpeg.exe"
    duplicate = source / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(replacement))
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", offline_bundle._sha256(duplicate))
    monkeypatch.setattr(offline_bundle, "verify_bundle", lambda *args, **kwargs: None)
    templates = tmp_path / "templates"
    backend = tmp_path / "backend"
    templates.mkdir()
    backend.mkdir()
    config = ReleaseConfig("bundle", "1", "poc", "opengv")

    bundle = assemble_bundle(
        config=config,
        runtime_archive=archive_path,
        backend_root=backend,
        templates_root=templates,
        output_dir=tmp_path / "dist",
        source_commit="commit",
        documentation_root=None,
        runtime_profile="conda-ffmpeg-only",
    )

    trace = json.loads((bundle / "runtime-profile.json").read_text(encoding="utf-8"))
    assert trace["runtime_profile"] == "conda-ffmpeg-only"
    assert trace["redistribution_status"] == "pending"
    assert trace["replacement"]["path"] == "runtime/Library/bin/ffmpeg.exe"
    assert trace["excluded"]["path"].endswith("ffmpeg-win-x86_64-v7.1.exe")
    assert trace["conda_unpack_script"]["path"] == "runtime/Scripts/conda-unpack-script.py"
    assert trace["conda_unpack_script"]["sha256_before"] != trace["conda_unpack_script"]["sha256_after"]
    assert (bundle / "runtime/Library/bin/ffmpeg.exe").is_file()
    assert not (bundle / "runtime/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe").exists()


@pytest.mark.parametrize(
    "duplicate_record",
    (
        "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe",
        r"Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe",
    ),
)
def test_profile_removes_only_duplicate_prefix_record_and_unpack_script_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_record: str,
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"replacement", duplicate=b"duplicate")
    keep_relative = "Lib/site-packages/keep.txt"
    keep = runtime / keep_relative
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("keep", encoding="utf-8")
    script = runtime / "Scripts/conda-unpack-script.py"
    script.write_text(
        "from pathlib import Path\n"
        f"_prefix_records = {[duplicate_record, keep_relative]!r}\n"
        "for record in _prefix_records:\n"
        "    with (Path(__file__).parents[1] / record).open('rb'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    replacement = runtime / "Library/bin/ffmpeg.exe"
    duplicate = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(replacement))
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", offline_bundle._sha256(duplicate))

    profile = offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert keep_relative in script.read_text(encoding="utf-8")
    assert "ffmpeg-win-x86_64-v7.1.exe" not in script.read_text(encoding="utf-8")
    assert profile["conda_unpack_script"]["sha256_before"] != profile["conda_unpack_script"]["sha256_after"]


def test_profile_rejects_unknown_unpack_script_before_removing_duplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    _write_profile_runtime(runtime, replacement=b"replacement", duplicate=b"duplicate")
    script = runtime / "Scripts/conda-unpack-script.py"
    script.write_text("def records():\n    return []\n", encoding="utf-8")
    replacement = runtime / "Library/bin/ffmpeg.exe"
    duplicate = runtime / "Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
    monkeypatch.setattr(offline_bundle, "_CONDA_FFMPEG_SHA256", offline_bundle._sha256(replacement))
    monkeypatch.setattr(offline_bundle, "_IMAGEIO_FFMPEG_SHA256", offline_bundle._sha256(duplicate))

    with pytest.raises(ValueError, match="_prefix_records"):
        offline_bundle._apply_runtime_profile(runtime, "conda-ffmpeg-only")

    assert duplicate.read_bytes() == b"duplicate"
    assert script.read_text(encoding="utf-8") == "def records():\n    return []\n"


def test_conda_ffmpeg_profile_preserves_existing_bundle_target(tmp_path: Path) -> None:
    config = ReleaseConfig("existing", "1", "poc", "opengv")
    target = tmp_path / "existing"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("preserve", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        assemble_bundle(
            config=config,
            runtime_archive=tmp_path / "unused.tar.gz",
            backend_root=tmp_path / "backend",
            templates_root=tmp_path / "templates",
            output_dir=tmp_path,
            source_commit="commit",
            runtime_profile="conda-ffmpeg-only",
        )

    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_runtime_profile_cli_is_opt_in_and_rejects_unknown_values() -> None:
    parser = offline_bundle.build_parser()
    common = [
        "assemble", "--runtime-archive", "runtime.tar.gz", "--backend-root", "backend",
        "--output-dir", "dist", "--source-commit", "commit",
    ]

    assert parser.parse_args(common).runtime_profile is None
    assert parser.parse_args([*common, "--runtime-profile", "conda-ffmpeg-only"]).runtime_profile == "conda-ffmpeg-only"
    with pytest.raises(SystemExit):
        parser.parse_args([*common, "--runtime-profile", "unknown"])


def test_profile_cli_rejects_existing_zip_before_creating_staging(tmp_path: Path) -> None:
    config_path = tmp_path / "release-config.json"
    config_path.write_text(
        json.dumps(
            {
                "bundle_name": "bundle",
                "application_version": "1",
                "poc_commit": "poc",
                "opengv_commit": "opengv",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "dist"
    output.mkdir()
    archive = output / "bundle.zip"
    archive.write_bytes(b"existing archive")

    with pytest.raises(ValueError, match="archive already exists"):
        offline_bundle.main(
            [
                "assemble",
                "--config", str(config_path),
                "--runtime-archive", str(tmp_path / "must-not-be-opened.tar.gz"),
                "--backend-root", str(tmp_path / "backend"),
                "--templates-root", str(tmp_path / "templates"),
                "--output-dir", str(output),
                "--source-commit", "commit",
                "--runtime-profile", "conda-ffmpeg-only",
                "--zip",
            ]
        )

    assert archive.read_bytes() == b"existing archive"
    assert not (output / "bundle").exists()


def test_profile_cli_exclusively_creates_zip_if_it_appears_during_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "release-config.json"
    config_path.write_text(
        json.dumps(
            {
                "bundle_name": "bundle",
                "application_version": "1",
                "poc_commit": "poc",
                "opengv_commit": "opengv",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "dist"

    def assemble_with_racing_archive(**kwargs: object) -> Path:
        bundle = output / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "payload.txt").write_text("new payload", encoding="utf-8")
        (output / "bundle.zip").write_bytes(b"racing archive")
        return bundle

    monkeypatch.setattr(offline_bundle, "assemble_bundle", assemble_with_racing_archive)

    with pytest.raises(FileExistsError):
        offline_bundle.main(
            [
                "assemble",
                "--config", str(config_path),
                "--runtime-archive", str(tmp_path / "runtime.tar.gz"),
                "--backend-root", str(tmp_path / "backend"),
                "--templates-root", str(tmp_path / "templates"),
                "--output-dir", str(output),
                "--source-commit", "commit",
                "--runtime-profile", "conda-ffmpeg-only",
                "--zip",
            ]
        )

    assert (output / "bundle.zip").read_bytes() == b"racing archive"
