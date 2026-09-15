from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Mapping
from uuid import uuid4


_POLICY_VERSION = 1
_SFM_SCRATCH_NAMES = (
    "images",
    "masks",
    "database.db",
    "sparse",
    "sparse_text_export",
)
_UNSUCCESSFUL_TERMINAL_STATUSES = {
    "failed",
    "interrupted",
    "cancelled",
    "superseded",
    "stale_input",
}
_RECLAIMABLE_SUCCESS_JOB_TYPES = {"clip_render", "scene_bridge"}
_DIAGNOSTIC_SUFFIXES = {".csv", ".json", ".log", ".md", ".txt", ".yaml", ".yml"}
_MAX_DIAGNOSTIC_BYTES = 8 * 1024 * 1024
_ADAPTER_LOG_TAIL_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RetentionReport:
    root: str
    reclaimed_bytes: int = 0
    removed_paths: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    status: str | None = None
    job_type: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "policy_version": _POLICY_VERSION,
            "root": self.root,
            "status": self.status,
            "job_type": self.job_type,
            "reclaimed_bytes": self.reclaimed_bytes,
            "removed_paths": list(self.removed_paths),
            "errors": list(self.errors),
        }


def prune_sfm_workspace(stage_dir: Path) -> RetentionReport:
    """删除 SfM 已正式导出后可重建的 COLMAP 临时工作区。"""

    root, error = _resolve_directory(stage_dir)
    if root is None:
        return RetentionReport(root=str(stage_dir), errors=(error,))
    reclaimed = 0
    removed: list[str] = []
    errors: list[str] = []
    for name in _SFM_SCRATCH_NAMES:
        target = root / name
        if not target.exists() and not target.is_symlink():
            continue
        unsafe = _unsafe_link(target)
        if unsafe is not None:
            errors.append(f"refusing to follow link or reparse point: {unsafe}")
            continue
        try:
            _require_child(root, target)
            size = _tree_bytes(target)
            _remove_path(target)
        except OSError as exc:
            errors.append(f"{name}: {exc}")
            continue
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
            continue
        reclaimed += size
        removed.append(name)
    return RetentionReport(
        root=str(root),
        reclaimed_bytes=reclaimed,
        removed_paths=tuple(removed),
        errors=tuple(errors),
    )


def reclaim_terminal_attempt(
    attempt_dir: Path,
    *,
    status: str,
    job_type: str,
    published_outputs: Mapping[str, str],
) -> RetentionReport:
    """按终态与发布契约清理 attempt，大型诊断不会无限保留。"""

    root, error = _resolve_directory(attempt_dir)
    if root is None:
        return RetentionReport(
            root=str(attempt_dir),
            errors=(error,),
            status=status,
            job_type=job_type,
        )
    empty = RetentionReport(root=str(root), status=status, job_type=job_type)
    if status == "success":
        if job_type not in _RECLAIMABLE_SUCCESS_JOB_TYPES:
            return empty
        publication_error = _validate_external_publications(root, published_outputs)
        if publication_error is not None:
            return RetentionReport(
                root=str(root),
                errors=(publication_error,),
                status=status,
                job_type=job_type,
            )
    elif status not in _UNSUCCESSFUL_TERMINAL_STATUSES:
        return empty

    unsafe = _unsafe_link(root)
    if unsafe is not None:
        report = RetentionReport(
            root=str(root),
            errors=(f"refusing to follow link or reparse point: {unsafe}",),
            status=status,
            job_type=job_type,
        )
        _write_report_best_effort(root, report)
        return report

    reclaimed = 0
    removed: list[str] = []
    errors: list[str] = []
    files, directories = _attempt_entries(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.name == "retention_report.json":
            continue
        try:
            size = path.stat().st_size
            if _keep_diagnostic(path, size):
                continue
            if path.name == "adapter.log" and size > _MAX_DIAGNOSTIC_BYTES:
                reclaimed += _keep_file_tail(path, _ADAPTER_LOG_TAIL_BYTES)
                removed.append(f"{relative}:trimmed")
                continue
            _require_child(root, path)
            path.unlink()
            reclaimed += size
            removed.append(relative)
        except OSError as exc:
            errors.append(f"{relative}: {exc}")
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass

    report = RetentionReport(
        root=str(root),
        reclaimed_bytes=reclaimed,
        removed_paths=tuple(removed),
        errors=tuple(errors),
        status=status,
        job_type=job_type,
    )
    _write_report_best_effort(root, report)
    return report


def _resolve_directory(path: Path) -> tuple[Path | None, str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        return None, str(exc)
    if not resolved.is_dir():
        return None, f"retention root is not a directory: {resolved}"
    if _is_link_or_reparse(path):
        return None, f"retention root is a link or reparse point: {path}"
    return resolved, ""


def _require_child(root: Path, target: Path) -> None:
    resolved = target.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"retention target escapes root: {target}") from exc
    if not relative.parts:
        raise ValueError("retention policy cannot remove its root")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _unsafe_link(path: Path) -> Path | None:
    if _is_link_or_reparse(path):
        return path
    if not path.is_dir():
        return None
    for current, directories, files in os.walk(path, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *files):
            candidate = current_path / name
            if _is_link_or_reparse(candidate):
                return candidate
    return None


def _tree_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for current, _directories, files in os.walk(path, followlinks=False):
        for name in files:
            candidate = Path(current) / name
            total += candidate.stat().st_size
    return total


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _validate_external_publications(
    attempt_root: Path, published_outputs: Mapping[str, str]
) -> str | None:
    if not published_outputs:
        return "successful cleanup requires durable published outputs"
    for name, value in published_outputs.items():
        try:
            published = Path(value).resolve(strict=True)
        except OSError as exc:
            return f"published output {name} is unavailable: {exc}"
        try:
            published.relative_to(attempt_root)
        except ValueError:
            continue
        return f"published output {name} remains inside the attempt"
    return None


def _attempt_entries(root: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    directories: list[Path] = []
    for current, child_directories, child_files in os.walk(
        root, topdown=False, followlinks=False
    ):
        current_path = Path(current)
        files.extend(current_path / name for name in child_files)
        directories.extend(current_path / name for name in child_directories)
    directories.sort(key=lambda item: len(item.parts), reverse=True)
    files.sort(key=lambda item: item.as_posix())
    return files, directories


def _keep_diagnostic(path: Path, size: int) -> bool:
    return path.suffix.lower() in _DIAGNOSTIC_SUFFIXES and size <= _MAX_DIAGNOSTIC_BYTES


def _keep_file_tail(path: Path, tail_bytes: int) -> int:
    size = path.stat().st_size
    kept = min(size, tail_bytes)
    with path.open("rb") as stream:
        stream.seek(size - kept)
        tail = stream.read()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(tail)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return size - kept


def _write_report_best_effort(root: Path, report: RetentionReport) -> None:
    path = root / "retention_report.json"
    temporary = root / f".retention-report.{uuid4().hex}.tmp"
    try:
        payload = (
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
