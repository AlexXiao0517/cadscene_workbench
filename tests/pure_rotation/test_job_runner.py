from __future__ import annotations

import json
from pathlib import Path

from cadscene.workflow.job_runner import build_stage_command


def test_pure_rotation_stage_uses_dedicated_cli_not_sfm(tmp_path: Path) -> None:
    dataset = "hover"
    data = tmp_path / "data" / dataset
    data.mkdir(parents=True)
    (data / "clip.mp4").write_bytes(b"video")
    (data / "dataset_manifest.json").write_text(json.dumps({"dataset": dataset, "video": {"path": f"data/{dataset}/clip.mp4"}, "cad": {"design_json": None}, "workflow": {"trajectory_mode": "pure_rotation"}}), encoding="utf-8")

    command = build_stage_command(tmp_path, dataset, "r1", "pure_rotation", {"backend_root": "C:/backend"})

    assert "cadscene.cli.run_pure_rotation" in command
    assert "cadscene.cli.run_sfm" not in command
    assert "--backend-root" in command
