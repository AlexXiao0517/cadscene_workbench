from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import external_msvc
import windows_offline_bundle as bundle


REQUIRED = sorted(
    (
        "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
        "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
        "msvcp140_codecvt_ids.dll", "concrt140.dll", "vcomp140.dll",
        "vcamp140.dll", "vccorlib140.dll", "vcruntime140_threads.dll",
    )
)
ALIASES = [
    ("msvcp140.dll", "runtime/Lib/site-packages/pycolmap.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"),
    ("msvcp140_2.dll", "runtime/Lib/site-packages/pycolmap.libs/msvcp140_2-fe68f6b61d8de0f75e5717671051586a.dll"),
    ("vcomp140.dll", "runtime/Lib/site-packages/pycolmap.libs/vcomp140-f96f3a14d88d8846f31f3ab38a490304.dll"),
    ("msvcp140.dll", "runtime/Lib/site-packages/pyproj.libs/msvcp140-d76d4b45e040cbc263297f5a5893a46c.dll"),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest(excluded: list[tuple[str, bytes]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "minimum_version": "14.51.36231.0",
        "required_dlls": REQUIRED,
        "aliases": [{"source": source, "target": target} for source, target in ALIASES],
        "excluded": [{"path": path, "sha256": digest(data)} for path, data in excluded],
    }


def write_manifest(path: Path, excluded: list[tuple[str, bytes]]) -> Path:
    path.write_text(json.dumps(manifest(excluded)), encoding="utf-8")
    return path


def make_bundle(tmp_path: Path, excluded: list[tuple[str, bytes]]) -> tuple[Path, Path]:
    root = tmp_path / "bundle"
    script = root / "runtime/Scripts/conda-unpack-script.py"
    script.parent.mkdir(parents=True)
    records = [path.removeprefix("runtime/") for path, _ in excluded] + ["Lib/keep.txt"]
    script.write_text(
        "from pathlib import Path\n"
        f"_prefix_records = {records!r}\n"
        "for record in _prefix_records:\n"
        "    with (Path(__file__).parents[1] / record).open('rb'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    (root / "runtime/Lib/keep.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "runtime/Lib/keep.txt").write_bytes(b"keep")
    for relative, data in excluded:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root, script


def test_apply_removes_audited_files_writes_manifest_and_keeps_unpack_runnable(tmp_path: Path) -> None:
    excluded = [("runtime/vcruntime140.dll", b"canonical"), (ALIASES[0][1], b"alias")]
    root, script = make_bundle(tmp_path, excluded)
    source = write_manifest(tmp_path / "input.json", excluded)

    external_msvc.apply_external_msvc_manifest(root, source)
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert all(not (root / relative).exists() for relative, _ in excluded)
    assert "Lib/keep.txt" in script.read_text(encoding="utf-8")
    assert "vcruntime140.dll" not in script.read_text(encoding="utf-8")
    assert json.loads((root / "external-msvc.json").read_text(encoding="utf-8")) == manifest(excluded)


def test_absent_audited_file_is_allowed_and_record_removed(tmp_path: Path) -> None:
    excluded = [("runtime/ucrtbase.dll", b"absent")]
    root, script = make_bundle(tmp_path, excluded)
    (root / excluded[0][0]).unlink()
    source = write_manifest(tmp_path / "input.json", excluded)

    external_msvc.apply_external_msvc_manifest(root, source)

    assert "ucrtbase.dll" not in script.read_text(encoding="utf-8")


def test_uppercase_runtime_path_removes_literal_prefix_record_and_script_runs(tmp_path: Path) -> None:
    excluded = [("RUNTIME/vcruntime140.dll", b"payload")]
    root, script = make_bundle(tmp_path, excluded)
    script.write_text(
        "from pathlib import Path\n"
        "_prefix_records = ['vcruntime140.dll', 'Lib/keep.txt']\n"
        "for record in _prefix_records:\n"
        "    with (Path(__file__).parents[1] / record).open('rb'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    source = write_manifest(tmp_path / "input.json", excluded)

    external_msvc.apply_external_msvc_manifest(root, source)
    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "vcruntime140.dll" not in script.read_text(encoding="utf-8")


def test_hash_mismatch_preserves_every_file_and_unpack_script(tmp_path: Path) -> None:
    excluded = [("runtime/vcruntime140.dll", b"one"), ("runtime/msvcp140.dll", b"two")]
    root, script = make_bundle(tmp_path, excluded)
    source = write_manifest(tmp_path / "input.json", excluded)
    (root / excluded[1][0]).write_bytes(b"unexpected")
    before = script.read_bytes()

    with pytest.raises(ValueError, match="SHA256"):
        external_msvc.apply_external_msvc_manifest(root, source)

    assert (root / excluded[0][0]).read_bytes() == b"one"
    assert (root / excluded[1][0]).read_bytes() == b"unexpected"
    assert script.read_bytes() == before


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema_version": 2}, "schema"),
        ({"minimum_version": "14.50.0.0"}, "minimum"),
        ({"required_dlls": REQUIRED[:-1]}, "required"),
        ({"aliases": []}, "aliases"),
        ({"excluded": [{"path": "../vcruntime140.dll", "sha256": "0" * 64}]}, "path"),
        ({"excluded": [{"path": "runtime\\vcruntime140.dll", "sha256": "0" * 64}]}, "path"),
        ({"excluded": [{"path": "runtime/evil.exe", "sha256": "0" * 64}]}, "basename"),
        ({"excluded": [{"path": "runtime/not-msvc.dll", "sha256": "0" * 64}]}, "basename"),
        ({"excluded": [{"path": "runtime/vcruntime140.dll:ads", "sha256": "0" * 64}]}, "path"),
        ({"excluded": [{"path": "runtime/vcruntime140.dll", "sha256": "x" * 64}]}, "SHA256"),
    ],
)
def test_manifest_validation_rejects_malformed_input(change: dict[str, object], message: str) -> None:
    payload = manifest([])
    payload.update(change)
    with pytest.raises(ValueError, match=message):
        external_msvc.validate_external_msvc_manifest(payload)


def test_duplicate_casefold_path_is_rejected() -> None:
    payload = manifest([])
    payload["excluded"] = [
        {"path": "runtime/vcruntime140.dll", "sha256": "0" * 64},
        {"path": "RUNTIME/VCRUNTIME140.DLL", "sha256": "0" * 64},
    ]
    with pytest.raises(ValueError, match="duplicate"):
        external_msvc.validate_external_msvc_manifest(payload)


@pytest.mark.parametrize("unsafe", ("runtime//vcruntime140.dll", "runtime/./vcruntime140.dll"))
def test_noncanonical_posix_path_is_rejected(unsafe: str) -> None:
    payload = manifest([])
    payload["excluded"] = [{"path": unsafe, "sha256": "0" * 64}]
    with pytest.raises(ValueError, match="canonical|path"):
        external_msvc.validate_external_msvc_manifest(payload)


def test_lstat_reparse_attribute_is_detected_on_python_310_311(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "junction"
    target.mkdir()
    original = Path.lstat

    def flagged(path: Path):
        result = original(path)
        if path == target:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return result

    monkeypatch.setattr(Path, "lstat", flagged)
    assert external_msvc._is_link(target)


def test_link_ancestor_rejected_before_changes(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "bundle"
    root.mkdir()
    try:
        (root / "runtime").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("link creation is unavailable")
    source = write_manifest(tmp_path / "input.json", [("runtime/vcruntime140.dll", b"x")])
    with pytest.raises(ValueError, match="link|reparse"):
        external_msvc.apply_external_msvc_manifest(root, source)


def test_manifest_output_link_is_preflighted_before_payload_deletion(tmp_path: Path) -> None:
    excluded = [("runtime/vcruntime140.dll", b"payload")]
    root, script = make_bundle(tmp_path, excluded)
    source = write_manifest(tmp_path / "input.json", excluded)
    external = tmp_path / "outside.json"
    external.write_text("outside", encoding="utf-8")
    try:
        (root / "external-msvc.json").symlink_to(external)
    except OSError:
        pytest.skip("link creation is unavailable")
    before_script = script.read_bytes()

    with pytest.raises(ValueError, match="link|reparse"):
        external_msvc.apply_external_msvc_manifest(root, source)

    assert (root / excluded[0][0]).read_bytes() == b"payload"
    assert script.read_bytes() == before_script
    assert external.read_text(encoding="utf-8") == "outside"


def test_manifest_output_reparse_flag_is_preflighted_before_payload_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    excluded = [("runtime/vcruntime140.dll", b"payload")]
    root, script = make_bundle(tmp_path, excluded)
    source = write_manifest(tmp_path / "input.json", excluded)
    output_manifest = root / "external-msvc.json"
    output_manifest.write_text("old", encoding="utf-8")
    before_script = script.read_bytes()
    original = external_msvc._is_link
    monkeypatch.setattr(
        external_msvc,
        "_is_link",
        lambda path: path == output_manifest or original(path),
    )

    with pytest.raises(ValueError, match="link|reparse"):
        external_msvc.apply_external_msvc_manifest(root, source)

    assert (root / excluded[0][0]).read_bytes() == b"payload"
    assert script.read_bytes() == before_script
    assert output_manifest.read_text(encoding="utf-8") == "old"


def test_manifest_output_directory_is_preflighted_before_payload_deletion(tmp_path: Path) -> None:
    excluded = [("runtime/vcruntime140.dll", b"payload")]
    root, script = make_bundle(tmp_path, excluded)
    source = write_manifest(tmp_path / "input.json", excluded)
    (root / "external-msvc.json").mkdir()
    before_script = script.read_bytes()

    with pytest.raises(ValueError, match="regular file"):
        external_msvc.apply_external_msvc_manifest(root, source)

    assert (root / excluded[0][0]).read_bytes() == b"payload"
    assert script.read_bytes() == before_script


def test_zip_guard_rejects_restored_alias_without_touching_output(tmp_path: Path) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")
    alias = root / ALIASES[0][1]
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.write_bytes(b"restored")
    output = tmp_path / "guarded.zip"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="Microsoft runtime"):
        bundle.write_zip64(root, output)

    assert output.read_bytes() == b"sentinel"


def test_zip_guard_rejects_unexpected_canonical_before_creating_output(tmp_path: Path) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")
    unexpected = root / "colmap/bin/vcruntime140.dll"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_bytes(b"unexpected")
    output = tmp_path / "guarded.zip"

    with pytest.raises(ValueError, match="Microsoft runtime"):
        bundle.write_zip64(root, output)

    assert not output.exists()


def test_zip_guard_rejects_linked_payload_before_touching_output(tmp_path: Path) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")
    outside = tmp_path / "outside.dll"
    outside.write_bytes(b"outside")
    linked = root / "runtime/linked.dll"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("link creation is unavailable")
    output = tmp_path / "linked.zip"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="link|reparse"):
        bundle.write_zip64(root, output)

    assert output.read_bytes() == b"sentinel"


def test_zip_guard_rejects_payload_reparse_flag_before_touching_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")
    payload = root / "runtime/ordinary.dll"
    payload.write_bytes(b"payload")
    original = external_msvc._is_link
    monkeypatch.setattr(
        external_msvc, "_is_link", lambda path: path == payload or original(path)
    )
    output = tmp_path / "linked.zip"
    output.write_bytes(b"sentinel")

    with pytest.raises(ValueError, match="link|reparse"):
        bundle.write_zip64(root, output)

    assert output.read_bytes() == b"sentinel"


def test_zip_guard_rejects_reparse_root_before_creating_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")
    original = external_msvc._is_link
    monkeypatch.setattr(external_msvc, "_is_link", lambda path: path == root or original(path))
    output = tmp_path / "root.zip"

    with pytest.raises(ValueError, match="link|reparse"):
        bundle.write_zip64(root, output)

    assert not output.exists()


def test_pristine_external_msvc_bundle_archives_normally(tmp_path: Path) -> None:
    root, _ = make_bundle(tmp_path, [])
    (root / "external-msvc.json").write_text(json.dumps(manifest([])), encoding="utf-8")

    output = bundle.write_zip64(root, tmp_path / "pristine.zip", exclusive=True)

    with zipfile.ZipFile(output) as archive:
        assert archive.read("bundle/runtime/Lib/keep.txt") == b"keep"
        assert "bundle/external-msvc.json" in archive.namelist()


def test_default_archive_without_manifest_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "ordinary"
    root.mkdir()
    (root / "vcruntime140.dll").write_bytes(b"allowed-by-default")
    output = bundle.write_zip64(root, tmp_path / "ordinary.zip")
    with zipfile.ZipFile(output) as archive:
        assert archive.read("ordinary/vcruntime140.dll") == b"allowed-by-default"


def test_default_archive_without_manifest_ignores_reparse_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ordinary"
    root.mkdir()
    (root / "payload.txt").write_bytes(b"ordinary")
    original = external_msvc._is_link
    monkeypatch.setattr(
        external_msvc, "_is_link", lambda path: path == root or original(path)
    )

    output = bundle.write_zip64(root, tmp_path / "ordinary.zip")

    with zipfile.ZipFile(output) as archive:
        assert archive.read("ordinary/payload.txt") == b"ordinary"


def test_cli_rejects_manifest_without_profile_before_touching_staging(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"bundle_name": "bundle", "application_version": "1", "poc_commit": "p", "opengv_commit": "o"}), encoding="utf-8")
    output = tmp_path / "dist"
    with pytest.raises(ValueError, match="runtime profile"):
        bundle.main(["assemble", "--config", str(config), "--runtime-archive", str(tmp_path / "missing.tar"), "--backend-root", str(tmp_path / "backend"), "--templates-root", str(tmp_path / "templates"), "--output-dir", str(output), "--source-commit", "x", "--external-msvc-manifest", str(tmp_path / "manifest.json")])
    assert not output.exists()


def test_assemble_api_rejects_manifest_without_profile_before_touching_staging(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    with pytest.raises(ValueError, match="runtime profile"):
        bundle.assemble_bundle(
            config=bundle.ReleaseConfig("bundle", "1", "p", "o"),
            runtime_archive=tmp_path / "missing.tar",
            backend_root=tmp_path / "backend",
            templates_root=tmp_path / "templates",
            output_dir=output,
            source_commit="x",
            external_msvc_manifest=tmp_path / "manifest.json",
        )
    assert not output.exists()


def test_direct_script_cli_exposes_external_manifest_option() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/windows_offline_bundle.py"), "assemble", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "--external-msvc-manifest" in completed.stdout
