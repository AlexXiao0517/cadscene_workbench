from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "packaging/windows/launcher/external-msvc.ps1"
START = ROOT / "packaging/windows/launcher/start.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
URL = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
REQUIRED = [
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "concrt140.dll", "vcomp140.dll",
    "vcamp140.dll", "vccorlib140.dll", "vcruntime140_threads.dll",
]
ALIASES = [
    ("msvcp140.dll", "runtime/Lib/site-packages/pycolmap.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"),
    ("msvcp140_2.dll", "runtime/Lib/site-packages/pycolmap.libs/msvcp140_2-fe68f6b61d8de0f75e5717671051586a.dll"),
    ("vcomp140.dll", "runtime/Lib/site-packages/pycolmap.libs/vcomp140-f96f3a14d88d8846f31f3ab38a490304.dll"),
    ("msvcp140.dll", "runtime/Lib/site-packages/pyproj.libs/msvcp140-d76d4b45e040cbc263297f5a5893a46c.dll"),
]


def _manifest(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "minimum_version": "14.51.36231.0",
        "required_dlls": REQUIRED,
        "aliases": [{"source": s, "target": t} for s, t in ALIASES],
        "excluded": [],
    }
    value.update(changes)
    return value


def _run(bundle: Path, system: Path, *, version="14.51.36231.0", arch="x64", signed=True):
    script = f"""
$ErrorActionPreference = 'Stop'
function Get-ExternalMsvcSystemDirectory {{ '{str(system).replace("'", "''")}' }}
function Get-ExternalMsvcFileVersion([string]$Path) {{ '{version}' }}
function Get-ExternalMsvcMachine([string]$Path) {{ '{arch}' }}
function Test-ExternalMsvcMicrosoftSignature([string]$Path) {{ ${str(signed).lower()} }}
. '{str(HELPER).replace("'", "''")}'
Initialize-ExternalMsvc -BundleRoot '{str(bundle).replace("'", "''")}'
"""
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True, capture_output=True, encoding="utf-8", errors="replace", check=False,
    )


@pytest.fixture()
def runtime(tmp_path: Path) -> tuple[Path, Path]:
    bundle, system = tmp_path / "bundle", tmp_path / "system32"
    bundle.mkdir(); system.mkdir()
    for name in REQUIRED:
        (system / name).write_bytes(("system:" + name).encode())
    return bundle, system


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_absent_manifest_is_noop(runtime: tuple[Path, Path]) -> None:
    bundle, system = runtime
    before = sorted(p.relative_to(bundle) for p in bundle.rglob("*"))
    result = _run(bundle, system)
    assert result.returncode == 0, result.stderr
    assert sorted(p.relative_to(bundle) for p in bundle.rglob("*")) == before


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
@pytest.mark.parametrize("failure", ["missing", "old", "x86", "unsigned"])
def test_invalid_installed_runtime_refuses_before_copy(runtime: tuple[Path, Path], failure: str) -> None:
    bundle, system = runtime
    (bundle / "external-msvc.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    if failure == "missing":
        (system / REQUIRED[-1]).unlink()
    kwargs = {"version": "14.50.0.0"} if failure == "old" else {}
    kwargs |= {"arch": "x86"} if failure == "x86" else {}
    kwargs |= {"signed": False} if failure == "unsigned" else {}
    result = _run(bundle, system, **kwargs)
    assert result.returncode != 0
    assert URL in result.stderr
    assert any(word in result.stderr for word in ("缺少", "版本", "x64", "签名"))
    assert not (bundle / "runtime").exists()


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_valid_runtime_copies_exact_aliases_and_is_idempotent(runtime: tuple[Path, Path]) -> None:
    bundle, system = runtime
    (bundle / "external-msvc.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    first = _run(bundle, system)
    assert first.returncode == 0, first.stderr
    files = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*.dll"))
    assert files == sorted(target for _, target in ALIASES)
    assert "不得重新分发" in first.stdout
    mtimes = {target: (bundle / target).stat().st_mtime_ns for _, target in ALIASES}
    second = _run(bundle, system)
    assert second.returncode == 0, second.stderr
    assert {target: (bundle / target).stat().st_mtime_ns for _, target in ALIASES} == mtimes
    for source, target in ALIASES:
        assert (bundle / target).read_bytes() == (system / source).read_bytes()


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_launcher_accepts_python_contract_higher_minimum_and_fixed_colmap_exclusion(
    runtime: tuple[Path, Path],
) -> None:
    bundle, system = runtime
    value = _manifest(
        minimum_version="14.52.0.0",
        excluded=[
            {"path": "runtime/Library/bin/api-ms-win-crt-runtime-l1-1-0.dll", "sha256": "0" * 64},
            {"path": "colmap/bin/vcruntime140.dll", "sha256": "0" * 64},
            {"path": "pure_rotation_backend/outputs/build_opengv_cli/ucrtbase.dll", "sha256": "0" * 64},
        ],
    )
    (bundle / "external-msvc.json").write_text(json.dumps(value), encoding="utf-8")

    result = _run(bundle, system, version="14.52.0.0")

    assert result.returncode == 0, result.stderr
    assert sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*.dll")) == sorted(
        target for _, target in ALIASES
    )


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_launcher_requires_installed_files_to_meet_manifest_minimum(runtime: tuple[Path, Path]) -> None:
    bundle, system = runtime
    (bundle / "external-msvc.json").write_text(
        json.dumps(_manifest(minimum_version="14.52.0.0")), encoding="utf-8"
    )

    result = _run(bundle, system, version="14.51.36231.0")

    assert result.returncode != 0
    assert URL in result.stderr
    assert not (bundle / "runtime").exists()


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
@pytest.mark.parametrize(
    "change",
    [
        {"aliases": [{"source": "evil.dll", "target": ALIASES[0][1]}]},
        {"aliases": [{"source": ALIASES[0][0], "target": "../escaped.dll"}]},
        {"aliases": [{"source": ALIASES[0][0], "target": "runtime/other.dll"}]},
        {"extra": True},
        {"schema_version": 2},
        {"excluded": [{"path": "../system.dll", "sha256": "bad"}]},
        {"aliases": [{"source": s, "target": t.upper()} for s, t in ALIASES]},
    ],
)
def test_unsafe_or_unknown_manifest_refuses_before_write(runtime: tuple[Path, Path], change: dict[str, object]) -> None:
    bundle, system = runtime
    (bundle / "external-msvc.json").write_text(json.dumps(_manifest(**change)), encoding="utf-8")
    result = _run(bundle, system)
    assert result.returncode != 0
    assert not list(bundle.rglob("*.dll"))


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_reparse_alias_ancestor_refuses_before_write(runtime: tuple[Path, Path]) -> None:
    bundle, system = runtime
    outside = bundle.parent / "outside"; outside.mkdir()
    runtime_dir = bundle / "runtime"; runtime_dir.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(runtime_dir / "Lib"), str(outside)], check=True, capture_output=True)
    (bundle / "external-msvc.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    result = _run(bundle, system)
    assert result.returncode != 0
    assert not list(outside.rglob("*.dll"))


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_existing_alias_is_atomically_replaced_from_system(runtime: tuple[Path, Path]) -> None:
    bundle, system = runtime
    (bundle / "external-msvc.json").write_text(json.dumps(_manifest()), encoding="utf-8")
    target = bundle / ALIASES[0][1]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"stale")
    result = _run(bundle, system)
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == (system / ALIASES[0][0]).read_bytes()
    assert not list(target.parent.glob("*.tmp"))


def test_launcher_initializes_external_runtime_before_python_or_workspace_changes() -> None:
    source = START.read_text(encoding="utf-8")
    helper = '. (Join-Path $PSScriptRoot "external-msvc.ps1")'
    call = "Initialize-ExternalMsvc -BundleRoot $bundleRoot"
    assert helper in source and call in source
    assert source.index(call) < source.index("Bundled Python runtime is missing")
    assert source.index(call) < source.index("conda-unpack-script.py")
    assert source.index(call) < source.index('New-Item -ItemType Directory -Force -Path $workspace')


@pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell is required")
def test_helper_syntax_loads() -> None:
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", f". '{HELPER}'"],
        text=True, capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
