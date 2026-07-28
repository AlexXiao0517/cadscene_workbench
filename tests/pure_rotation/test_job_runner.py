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

    command = build_stage_command(tmp_path, dataset, "r1", "pure_rotation", {"backend_root": "C:/backend", "cadscene_readonly": "C:/readonly"})

    assert "cadscene.cli.run_pure_rotation" in command
    assert "cadscene.cli.run_sfm" not in command
    assert "--backend-root" in command
    assert command[command.index("--cadscene-readonly") + 1] == "C:/readonly"


def test_pure_rotation_force_rerun_reaches_dedicated_cli(tmp_path: Path) -> None:
    dataset = "hover"
    data = tmp_path / "data" / dataset
    data.mkdir(parents=True)
    (data / "clip.mp4").write_bytes(b"video")
    (data / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "video": {"path": f"data/{dataset}/clip.mp4"},
                "cad": {"design_json": None},
                "workflow": {"trajectory_mode": "pure_rotation"},
            }
        ),
        encoding="utf-8",
    )

    command = build_stage_command(
        tmp_path,
        dataset,
        "r1",
        "pure_rotation",
        {"backend_root": "C:/backend", "force": True},
    )

    assert "--force" in command


def test_pure_rotation_render_uses_corrected_track_without_sfm(tmp_path: Path) -> None:
    dataset = "hover"
    data = tmp_path / "data" / dataset
    cad = data / "cad"
    cad.mkdir(parents=True)
    (data / "clip.mp4").write_bytes(b"video")
    (cad / "design.json").write_text('{"meta":{"bbox":{"min_x":0,"min_y":0,"max_x":1,"max_y":1}},"layers":[]}', encoding="utf-8")
    (data / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset": dataset,
                "video": {"path": f"data/{dataset}/clip.mp4"},
                "cad": {"design_json": f"data/{dataset}/cad/design.json"},
                "defaults": {"cad_scale": 1.0, "origin_xy": [0.0, 0.0]},
                "workflow": {"trajectory_mode": "pure_rotation"},
            }
        ),
        encoding="utf-8",
    )
    corrected = tmp_path / "runs" / dataset / "r1" / "04_pure_rotation_corrections"
    corrected.mkdir(parents=True)
    (corrected / "camera_track_corrected.json").write_text('{"poses":[]}', encoding="utf-8")

    command = build_stage_command(tmp_path, dataset, "r1", "render")

    assert "cadscene.cli.render_pure_rotation" in command
    assert "cadscene.cli.run_pipeline" not in command
    assert command[command.index("--track") + 1].endswith("camera_track_corrected.json")
