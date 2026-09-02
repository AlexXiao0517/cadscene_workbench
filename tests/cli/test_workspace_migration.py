from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cadscene.cli.workspace_migration import (
    MARKER_NAME,
    WorkspaceMigrationError,
    build_migration_plan,
    record_migration,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _copied_workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    old_workspace = tmp_path / "old-bundle" / "workspace"
    old_project = old_workspace / "projects" / "p-test"
    old_asset = old_project / "assets" / "video.mp4"
    old_data = old_workspace / "data" / "cad-test" / "design.json"
    old_run = old_workspace / "runs" / "p-test" / "clip-0001" / "manifest.json"
    old_asset.parent.mkdir(parents=True)
    old_asset.write_bytes(b"video")
    _write_json(old_data, {"kind": "cad"})
    _write_json(old_run, {"kind": "run"})
    _write_json(
        old_project / "project_manifest.json",
        {
            "project_id": "p-test",
            "video": str(old_asset),
            "cad_dataset": str(old_data.parent),
            "run_manifest": str(old_run),
        },
    )
    _write_json(
        old_project / "clips_manifest.json",
        {"clips": [{"clip_id": "clip-0001", "video": str(old_asset)}]},
    )

    new_workspace = tmp_path / "new-bundle" / "workspace"
    shutil.copytree(old_workspace, new_workspace)
    return old_workspace, new_workspace, old_asset


def test_empty_bundle_workspace_needs_no_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "new-bundle" / "workspace"
    workspace.mkdir(parents=True)

    plan = build_migration_plan(workspace)

    assert plan.action == "none"
    assert plan.workspace == workspace.resolve()
    assert plan.old_workspace is None


def test_copied_workspace_plans_recoverable_compatibility_junction(tmp_path: Path) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)

    plan = build_migration_plan(
        new_workspace,
        now=datetime(2026, 9, 2, 10, 11, 12, tzinfo=timezone.utc),
    )

    assert plan.action == "migrate"
    assert plan.old_workspace == old_workspace.resolve()
    assert plan.backup_workspace == old_workspace.with_name(
        "workspace.pre-0.1.3-20260902-101112"
    ).resolve()
    assert plan.referenced_path_count == 3
    assert plan.project_count == 1


def test_workspace_with_paths_from_multiple_old_bundles_is_rejected(tmp_path: Path) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    second_workspace = tmp_path / "another-bundle" / "workspace"
    second_asset = second_workspace / "projects" / "p-other" / "assets" / "other.mp4"
    second_asset.parent.mkdir(parents=True)
    second_asset.write_bytes(b"other")
    _write_json(
        new_workspace / "projects" / "p-test" / "render_manifest.json",
        {"foreign_video": str(second_asset)},
    )

    with pytest.raises(WorkspaceMigrationError, match="多个旧 workspace"):
        build_migration_plan(new_workspace)

    assert old_workspace.is_dir()


def test_incomplete_workspace_copy_is_rejected_before_old_workspace_moves(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, old_asset = _copied_workspace(tmp_path)
    copied_asset = new_workspace / old_asset.relative_to(old_workspace)
    copied_asset.unlink()

    with pytest.raises(WorkspaceMigrationError, match="复制不完整"):
        build_migration_plan(new_workspace)

    assert old_workspace.is_dir()


def test_changed_project_manifest_is_rejected_before_old_workspace_moves(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    _write_json(
        new_workspace / "projects" / "p-test" / "clips_manifest.json",
        {"clips": []},
    )

    with pytest.raises(WorkspaceMigrationError, match="项目清单不一致"):
        build_migration_plan(new_workspace)

    assert old_workspace.is_dir()


def test_destination_only_project_is_reported_before_old_workspace_moves(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    _write_json(
        new_workspace / "projects" / "p-new" / "project_manifest.json",
        {"project_id": "p-new"},
    )

    with pytest.raises(
        WorkspaceMigrationError,
        match=r"新版额外.*projects/p-new/project_manifest\.json",
    ):
        build_migration_plan(new_workspace)

    assert old_workspace.is_dir()


def test_changed_project_manifest_error_identifies_conflicting_file(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    _write_json(
        new_workspace / "projects" / "p-test" / "clips_manifest.json",
        {"clips": []},
    )

    with pytest.raises(
        WorkspaceMigrationError,
        match=r"内容冲突.*projects/p-test/clips_manifest\.json",
    ):
        build_migration_plan(new_workspace)

    assert old_workspace.is_dir()


def test_existing_marker_requires_old_junction_to_target_current_workspace(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    backup = old_workspace.with_name("workspace.pre-0.1.3-20260902-101112")
    marker = {
        "schema_version": 1,
        "old_workspace": str(old_workspace.resolve()),
        "workspace": str(new_workspace.resolve()),
        "backup_workspace": str(backup.resolve()),
        "migrated_at": "2026-09-02T10:11:12+00:00",
    }
    _write_json(new_workspace / MARKER_NAME, marker)

    with pytest.raises(WorkspaceMigrationError, match="兼容 Junction 已失效"):
        build_migration_plan(new_workspace, same_path=lambda _a, _b: False)

    plan = build_migration_plan(new_workspace, same_path=lambda _a, _b: True)
    assert plan.action == "none"
    assert plan.old_workspace == old_workspace.resolve()


def test_record_migration_writes_marker_only_after_junction_verification(
    tmp_path: Path,
) -> None:
    old_workspace, new_workspace, _ = _copied_workspace(tmp_path)
    backup = old_workspace.with_name("workspace.pre-0.1.3-20260902-101112")

    with pytest.raises(WorkspaceMigrationError, match="Junction"):
        record_migration(
            new_workspace,
            old_workspace,
            backup,
            same_path=lambda _a, _b: False,
        )
    assert not (new_workspace / MARKER_NAME).exists()

    record_migration(
        new_workspace,
        old_workspace,
        backup,
        same_path=lambda _a, _b: True,
        now=datetime(2026, 9, 2, 10, 11, 12, tzinfo=timezone.utc),
    )
    payload = json.loads((new_workspace / MARKER_NAME).read_text(encoding="utf-8"))
    assert payload["old_workspace"] == str(old_workspace.resolve())
    assert payload["backup_workspace"] == str(backup.resolve())
    assert payload["migrated_at"] == "2026-09-02T10:11:12+00:00"
