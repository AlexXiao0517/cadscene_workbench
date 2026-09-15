"""Validate and apply the public bundle's audited external-MSVC manifest."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Iterable


SCHEMA_VERSION = 1
MINIMUM_VERSION = (14, 51, 36231, 0)
REQUIRED_DLLS = frozenset(
    {
        "concrt140.dll", "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
        "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll", "vcamp140.dll",
        "vccorlib140.dll", "vcomp140.dll", "vcruntime140.dll",
        "vcruntime140_1.dll", "vcruntime140_threads.dll",
    }
)
ALIASES = {
    "runtime/Lib/site-packages/pycolmap.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll": "msvcp140.dll",
    "runtime/Lib/site-packages/pycolmap.libs/msvcp140_2-fe68f6b61d8de0f75e5717671051586a.dll": "msvcp140_2.dll",
    "runtime/Lib/site-packages/pycolmap.libs/vcomp140-f96f3a14d88d8846f31f3ab38a490304.dll": "vcomp140.dll",
    "runtime/Lib/site-packages/pyproj.libs/msvcp140-d76d4b45e040cbc263297f5a5893a46c.dll": "msvcp140.dll",
}
_ALIAS_CASEFOLD = {path.casefold(): source for path, source in ALIASES.items()}
_ALIAS_BASENAMES = {PurePosixPath(path).name.casefold() for path in ALIASES}
_API_SET = re.compile(r"api-ms-win-(?:core|crt)-[a-z0-9-]+\.dll", re.IGNORECASE)
_HASHED_RUNTIME = re.compile(
    r"(?:concrt140|msvcp140(?:_[12]|_atomic_wait|_codecvt_ids)?|vcamp140|"
    r"vccorlib140|vcomp140|vcruntime140(?:_1|_threads)?)-[0-9a-f]{32}\.dll",
    re.IGNORECASE,
)
_ALLOWED_PARENTS = (
    "runtime",
    "runtime/library/bin",
    "runtime/lib/site-packages/pycolmap.libs",
    "runtime/lib/site-packages/pyproj.libs",
    "colmap/bin",
    "pure_rotation_backend/outputs/build_opengv_cli",
)


def _version(value: object) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){3}", value):
        raise ValueError("external MSVC minimum_version is invalid")
    return tuple(int(part) for part in value.split("."))


def _runtime_basename_allowed(name: str) -> bool:
    folded = name.casefold()
    return (
        folded in REQUIRED_DLLS
        or folded == "ucrtbase.dll"
        or bool(_API_SET.fullmatch(name))
        or folded in _ALIAS_BASENAMES
    )


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("external MSVC excluded path is invalid")
    path = PurePosixPath(value)
    if path.as_posix() != value:
        raise ValueError("external MSVC excluded path is not canonical POSIX")
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("external MSVC excluded path is invalid")
    parent = path.parent.as_posix().casefold()
    if parent not in _ALLOWED_PARENTS:
        raise ValueError("external MSVC excluded path is outside fixed binary directories")
    if not _runtime_basename_allowed(path.name):
        raise ValueError(f"unknown Microsoft runtime basename: {path.name}")
    if path.name.casefold() in _ALIAS_BASENAMES and value.casefold() not in _ALIAS_CASEFOLD:
        raise ValueError(f"unknown Microsoft runtime basename or alias path: {value}")
    return value


def validate_external_msvc_manifest(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "minimum_version", "required_dlls", "aliases", "excluded"
    }:
        raise ValueError("external MSVC manifest schema is invalid")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("external MSVC manifest schema version is invalid")
    if _version(payload["minimum_version"]) < MINIMUM_VERSION:
        raise ValueError("external MSVC minimum version is below the supported minimum")
    required = payload["required_dlls"]
    if not isinstance(required, list) or len(required) != len(REQUIRED_DLLS) or set(required) != REQUIRED_DLLS:
        raise ValueError("external MSVC required DLL set is invalid")
    aliases = payload["aliases"]
    if not isinstance(aliases, list):
        raise ValueError("external MSVC aliases set is invalid")
    actual_aliases: dict[str, str] = {}
    for alias in aliases:
        if not isinstance(alias, dict) or set(alias) != {"source", "target"}:
            raise ValueError("external MSVC aliases set is invalid")
        source, target = alias["source"], alias["target"]
        if not isinstance(source, str) or not isinstance(target, str) or target in actual_aliases:
            raise ValueError("external MSVC aliases set is invalid")
        actual_aliases[target] = source
    if actual_aliases != ALIASES:
        raise ValueError("external MSVC aliases set is invalid")
    excluded = payload["excluded"]
    if not isinstance(excluded, list):
        raise ValueError("external MSVC excluded records are invalid")
    seen: set[str] = set()
    for record in excluded:
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise ValueError("external MSVC excluded record schema is invalid")
        relative = _validate_relative_path(record["path"])
        folded = relative.casefold()
        if folded in seen:
            raise ValueError("duplicate external MSVC excluded path")
        seen.add(folded)
        if not isinstance(record["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]):
            raise ValueError("external MSVC excluded SHA256 is invalid")
    return payload


def load_external_msvc_manifest(path: str | Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read external MSVC manifest: {error}") from error
    return validate_external_msvc_manifest(payload)


def _is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _plain_target(root: Path, relative: str) -> Path:
    if _is_link(root):
        raise ValueError("external MSVC bundle root is a link or reparse point")
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if _is_link(current):
            raise ValueError(f"external MSVC path has a link or reparse ancestor: {relative}")
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def filtered_conda_prefix_script(script: Path, removed: Iterable[str]) -> bytes:
    source = script.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(script))
    except SyntaxError as error:
        raise ValueError("conda-unpack-script.py has unknown _prefix_records structure") from error
    assignments = [node for node in tree.body if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_prefix_records"]
    if len(assignments) != 1:
        raise ValueError("conda-unpack-script.py has unknown _prefix_records structure")
    assignment = assignments[0]
    try:
        records = ast.literal_eval(assignment.value)
    except (ValueError, TypeError, SyntaxError) as error:
        raise ValueError("conda-unpack-script.py has unknown _prefix_records structure") from error
    if not isinstance(records, (list, tuple)):
        raise ValueError("conda-unpack-script.py has unknown _prefix_records structure")
    def record_path(record: object) -> str | None:
        if isinstance(record, str):
            return record
        if isinstance(record, (list, tuple)) and record and isinstance(record[0], str):
            return record[0]
        return None
    if any(record_path(record) is None for record in records):
        raise ValueError("conda-unpack-script.py has unknown _prefix_records structure")
    omitted = {value.replace("\\", "/").casefold() for value in removed}
    filtered = [record for record in records if record_path(record).replace("\\", "/").casefold() not in omitted]
    if filtered == list(records):
        return script.read_bytes()
    source_bytes = source.encode("utf-8")
    lines = source.splitlines(keepends=True)
    start = sum(len(line.encode("utf-8")) for line in lines[: assignment.lineno - 1]) + assignment.col_offset
    end = sum(len(line.encode("utf-8")) for line in lines[: assignment.end_lineno - 1]) + assignment.end_col_offset
    return source_bytes[:start] + f"_prefix_records = {filtered!r}".encode() + source_bytes[end:]


def apply_external_msvc_manifest(bundle_root: str | Path, manifest_path: str | Path) -> dict[str, object]:
    root = Path(bundle_root)
    payload = load_external_msvc_manifest(manifest_path)
    output_manifest = _plain_target(root, "external-msvc.json")
    if output_manifest.exists() and not output_manifest.is_file():
        raise ValueError("external-msvc.json destination is not a regular file")
    records = payload["excluded"]
    targets: list[Path] = []
    for record in records:
        target = _plain_target(root, record["path"])
        if target.exists():
            if not target.is_file() or _sha256(target).casefold() != record["sha256"].casefold():
                raise ValueError(f"external MSVC excluded file has unexpected SHA256: {record['path']}")
        targets.append(target)
    script = _plain_target(root, "runtime/Scripts/conda-unpack-script.py")
    if not script.is_file():
        raise ValueError("conda-unpack-script.py is missing")
    runtime_records = [
        record["path"].split("/", 1)[1]
        for record in records
        if record["path"].casefold().startswith("runtime/")
    ]
    updated_script = filtered_conda_prefix_script(script, runtime_records)
    for target in targets:
        if target.exists():
            target.unlink()
    script.write_bytes(updated_script)
    output_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def validate_external_msvc_archive_root(bundle_root: str | Path) -> None:
    root = Path(bundle_root)
    lexical_manifest = root / "external-msvc.json"
    try:
        lexical_manifest.lstat()
    except FileNotFoundError:
        return
    manifest_path = _plain_target(root, "external-msvc.json")
    if not manifest_path.is_file():
        raise ValueError("external-msvc.json is not a regular file")
    payload = load_external_msvc_manifest(manifest_path)
    forbidden_paths = {record["path"].casefold() for record in payload["excluded"]} | set(_ALIAS_CASEFOLD)
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            relative = path.relative_to(root).as_posix()
            _plain_target(root, relative)
            if path.is_dir():
                pending.append(path)
                continue
            if not path.is_file():
                continue
            name = path.name
            if relative.casefold() in forbidden_paths or _runtime_basename_allowed(name) or _HASHED_RUNTIME.fullmatch(name):
                if relative.casefold() != "external-msvc.json":
                    raise ValueError(f"Microsoft runtime payload must not be archived: {relative}")
