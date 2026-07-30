from __future__ import annotations

from pathlib import Path

import pytest

from cadscene.cli.run_pure_rotation import promote_output_tree


def test_promote_output_tree_replaces_existing_result_only_when_forced(tmp_path: Path) -> None:
    destination = tmp_path / "02_pure_rotation"
    destination.mkdir()
    (destination / "result.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / ".rerun" / "02_pure_rotation"
    staged.mkdir(parents=True)
    (staged / "result.txt").write_text("new", encoding="utf-8")

    promote_output_tree(staged, destination, force=True)

    assert (destination / "result.txt").read_text(encoding="utf-8") == "new"
    assert not staged.exists()
    assert not list(tmp_path.glob(".02_pure_rotation.backup-*"))


def test_promote_output_tree_preserves_existing_result_without_force(tmp_path: Path) -> None:
    destination = tmp_path / "02_pure_rotation"
    destination.mkdir()
    (destination / "result.txt").write_text("old", encoding="utf-8")
    staged = tmp_path / ".rerun" / "02_pure_rotation"
    staged.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="already exists"):
        promote_output_tree(staged, destination, force=False)

    assert (destination / "result.txt").read_text(encoding="utf-8") == "old"
    assert staged.exists()
