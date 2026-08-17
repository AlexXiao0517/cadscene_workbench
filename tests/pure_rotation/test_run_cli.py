from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from cadscene.cli import run_pure_rotation
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


def test_run_pure_rotation_reports_determinate_progress_until_validation(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    progress_file = tmp_path / "adapter_progress.json"
    reported: list[tuple[str, float]] = []

    def fake_run_video(self, *, video, cadscene_readonly, output_dir, **_kwargs):
        output_dir.mkdir(parents=True)
        (output_dir / "full_video_rotation_trajectory.json").write_text(
            json.dumps({"poses": []}), encoding="utf-8"
        )
        (output_dir / "full_video_pairwise_rotations.csv").write_text(
            "decoded_frame_index_1,decoded_frame_index_2\n", encoding="utf-8"
        )
        return {"summary": {"ok": True}}

    monkeypatch.setattr(
        run_pure_rotation.ExternalOpenGVBackend,
        "run_video",
        fake_run_video,
    )
    monkeypatch.setattr(
        run_pure_rotation,
        "convert_poc_raw_trajectory",
        lambda *_args, **_kwargs: {"poses": []},
    )
    monkeypatch.setattr(
        run_pure_rotation,
        "_write_progress",
        lambda _path, stage, _message, fraction: reported.append(
            (stage, fraction)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pure_rotation",
            "--dataset",
            "p1",
            "--run-id",
            "c1",
            "--output-root",
            str(tmp_path / "output"),
            "--video",
            str(video),
            "--progress-file",
            str(progress_file),
        ],
    )

    assert run_pure_rotation.main() == 0
    assert reported == [
        ("pure_rotation", 0.05),
        ("converting", 0.90),
        ("publishing", 0.96),
        ("ready_for_validation", 0.98),
    ]
