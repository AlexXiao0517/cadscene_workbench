from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Sequence


MARKER_NAME = ".cadscene-workspace-migration.json"
_WORKSPACE_CHILDREN = frozenset({"projects", "data", "runs"})
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_SamePath = Callable[[Path, Path], bool]


class WorkspaceMigrationError(RuntimeError):
    """工作空间升级不能在不破坏历史状态的前提下继续。"""


@dataclass(frozen=True)
class MigrationPlan:
    action: str
    workspace: Path
    old_workspace: Path | None = None
    backup_workspace: Path | None = None
    project_count: int = 0
    referenced_path_count: int = 0

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for key in ("workspace", "old_workspace", "backup_workspace"):
            value = payload[key]
            payload[key] = str(value) if value is not None else None
        return payload


def _same_path(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except (FileNotFoundError, OSError):
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second)
        )


def _absolute_path(path: Path) -> Path:
    # 不能使用 Path.resolve()：它会跟随 Junction，导致迁移标记丢失旧路径。
    return Path(os.path.abspath(path))


def _iter_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)


def _durable_json_files(workspace: Path) -> list[Path]:
    files = list(workspace.glob("*.json"))
    projects = workspace / "projects"
    if projects.is_dir():
        for project in projects.iterdir():
            if not project.is_dir():
                continue
            files.extend(project.glob("*.json"))
            assets = project / "assets"
            if assets.is_dir():
                files.extend(assets.glob("*.json"))
    marker = workspace / MARKER_NAME
    return sorted(path for path in files if path != marker)


def _workspace_root_from_value(value: str) -> Path | None:
    if not _WINDOWS_ABSOLUTE_PATH.match(value):
        return None
    path = PureWindowsPath(value)
    parts = path.parts
    for index in range(len(parts) - 1):
        if (
            parts[index].casefold() == "workspace"
            and parts[index + 1].casefold() in _WORKSPACE_CHILDREN
        ):
            return _absolute_path(Path(PureWindowsPath(*parts[: index + 1])))
    return None


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceMigrationError(f"无法读取工作空间清单：{path}（{exc}）") from exc


def _discover_references(workspace: Path) -> tuple[set[Path], set[Path]]:
    roots: set[Path] = set()
    references: set[Path] = set()
    for json_path in _durable_json_files(workspace):
        for value in _iter_strings(_load_json(json_path)):
            root = _workspace_root_from_value(value)
            if root is None:
                continue
            roots.add(root)
            references.add(_absolute_path(Path(PureWindowsPath(value))))
    return roots, references


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _manifest_inventory(workspace: Path) -> dict[str, str]:
    projects = workspace / "projects"
    inventory: dict[str, str] = {}
    if not projects.is_dir():
        return inventory
    for project in sorted(path for path in projects.iterdir() if path.is_dir()):
        for manifest in sorted(project.glob("*.json")):
            relative = manifest.relative_to(workspace).as_posix()
            try:
                inventory[relative] = hashlib.sha256(manifest.read_bytes()).hexdigest()
            except OSError as exc:
                raise WorkspaceMigrationError(
                    f"无法校验项目清单：{manifest}（{exc}）"
                ) from exc
    return inventory


def _validate_copy(
    old_workspace: Path,
    workspace: Path,
    references: set[Path],
) -> None:
    if not old_workspace.is_dir():
        raise WorkspaceMigrationError(
            f"检测到旧项目路径，但旧 workspace 不存在：{old_workspace}。"
            "请保留旧整合包并重新完整复制 workspace。"
        )
    if old_workspace.name.casefold() != "workspace":
        raise WorkspaceMigrationError(f"拒绝处理非 workspace 目录：{old_workspace}")

    old_inventory = _manifest_inventory(old_workspace)
    new_inventory = _manifest_inventory(workspace)
    if not old_inventory:
        raise WorkspaceMigrationError(
            "旧 workspace 中没有可校验的项目清单；为避免覆盖进度，迁移已停止。"
            "请关闭旧版服务后重新完整复制整个 workspace。"
        )
    missing_manifests = sorted(old_inventory.keys() - new_inventory.keys())
    extra_manifests = sorted(new_inventory.keys() - old_inventory.keys())
    changed_manifests = sorted(
        relative
        for relative in old_inventory.keys() & new_inventory.keys()
        if old_inventory[relative] != new_inventory[relative]
    )
    if missing_manifests or extra_manifests or changed_manifests:
        differences: list[str] = []
        if missing_manifests:
            differences.append(f"缺失：{'、'.join(missing_manifests[:5])}")
        if extra_manifests:
            differences.append(f"新版额外：{'、'.join(extra_manifests[:5])}")
        if changed_manifests:
            differences.append(f"内容冲突：{'、'.join(changed_manifests[:5])}")
        raise WorkspaceMigrationError(
            f"新旧 workspace 的项目清单不一致（{'；'.join(differences)}）；"
            "为避免覆盖进度，迁移已停止。"
            "请关闭旧版服务后重新完整复制整个 workspace。"
        )

    old_key = _path_key(old_workspace)
    missing: list[Path] = []
    for reference in sorted(references, key=str):
        try:
            relative = reference.relative_to(old_workspace)
        except ValueError:
            continue
        if _path_key(reference).startswith(old_key) and reference.exists():
            copied = workspace / relative
            if not copied.exists():
                missing.append(copied)
                if len(missing) >= 5:
                    break
    if missing:
        examples = "；".join(str(path) for path in missing)
        raise WorkspaceMigrationError(
            f"workspace 复制不完整，以下旧文件在新副本中缺失：{examples}。"
            "请重新复制整个 workspace 后再启动。"
        )


def _load_marker(workspace: Path) -> dict[str, object] | None:
    marker_path = workspace / MARKER_NAME
    if not marker_path.is_file():
        return None
    payload = _load_json(marker_path)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise WorkspaceMigrationError(f"工作空间迁移标记格式无效：{marker_path}")
    return payload


def build_migration_plan(
    workspace: Path,
    *,
    same_path: _SamePath = _same_path,
    now: datetime | None = None,
) -> MigrationPlan:
    workspace = _absolute_path(workspace)
    marker = _load_marker(workspace)
    if marker is not None:
        marker_workspace = _absolute_path(Path(str(marker.get("workspace", ""))))
        old_workspace = _absolute_path(Path(str(marker.get("old_workspace", ""))))
        if _path_key(marker_workspace) != _path_key(workspace):
            raise WorkspaceMigrationError(
                "迁移标记属于另一个 workspace；请恢复原目录结构或重新复制旧 workspace。"
            )
        if not same_path(old_workspace, workspace):
            raise WorkspaceMigrationError(
                f"旧路径兼容 Junction 已失效：{old_workspace}。"
                "请不要删除旧路径处的 Junction；重新复制旧 workspace 后再启动。"
            )
        backup_text = marker.get("backup_workspace")
        backup = _absolute_path(Path(str(backup_text))) if backup_text else None
        return MigrationPlan(
            action="none",
            workspace=workspace,
            old_workspace=old_workspace,
            backup_workspace=backup,
        )

    roots, references = _discover_references(workspace)
    current_key = _path_key(workspace)
    old_roots = sorted(
        (root for root in roots if _path_key(root) != current_key), key=str
    )
    if not old_roots:
        return MigrationPlan(action="none", workspace=workspace)
    if len(old_roots) != 1:
        listed = "；".join(str(path) for path in old_roots)
        raise WorkspaceMigrationError(
            f"检测到多个旧 workspace（{listed}），无法安全判断项目来源。"
            "请只复制一个旧版本的完整 workspace。"
        )

    old_workspace = old_roots[0]
    old_references = {
        path
        for path in references
        if _path_key(path).startswith(_path_key(old_workspace))
    }
    _validate_copy(old_workspace, workspace, old_references)
    project_count = len(list((workspace / "projects").glob("*/project_manifest.json")))
    if same_path(old_workspace, workspace):
        return MigrationPlan(
            action="record",
            workspace=workspace,
            old_workspace=old_workspace,
            project_count=project_count,
            referenced_path_count=len(old_references),
        )

    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    backup = _absolute_path(
        old_workspace.with_name(f"workspace.pre-0.1.3-{timestamp}")
    )
    if backup.exists():
        raise WorkspaceMigrationError(f"迁移备份目录已存在：{backup}")
    return MigrationPlan(
        action="migrate",
        workspace=workspace,
        old_workspace=old_workspace,
        backup_workspace=backup,
        project_count=project_count,
        referenced_path_count=len(old_references),
    )


def record_migration(
    workspace: Path,
    old_workspace: Path,
    backup_workspace: Path | None,
    *,
    same_path: _SamePath = _same_path,
    now: datetime | None = None,
) -> Path:
    workspace = _absolute_path(workspace)
    old_workspace = _absolute_path(old_workspace)
    if not same_path(old_workspace, workspace):
        raise WorkspaceMigrationError(
            f"兼容 Junction 未正确指向新 workspace：{old_workspace} -> {workspace}"
        )
    marker_path = workspace / MARKER_NAME
    payload = {
        "schema_version": 1,
        "old_workspace": str(old_workspace),
        "workspace": str(workspace),
        "backup_workspace": (
            str(_absolute_path(backup_workspace)) if backup_workspace is not None else None
        ),
        "migrated_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    temporary = marker_path.with_name(f".{marker_path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, marker_path)
    finally:
        temporary.unlink(missing_ok=True)
    return marker_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CADScene 同机 workspace 升级检查")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--workspace", type=Path, required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--workspace", type=Path, required=True)
    record.add_argument("--old-workspace", type=Path, required=True)
    record.add_argument("--backup-workspace", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "plan":
            plan = build_migration_plan(arguments.workspace)
            print(json.dumps(plan.to_json_dict(), ensure_ascii=False))
        else:
            marker = record_migration(
                arguments.workspace,
                arguments.old_workspace,
                arguments.backup_workspace,
            )
            print(json.dumps({"marker": str(marker)}, ensure_ascii=False))
    except WorkspaceMigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
