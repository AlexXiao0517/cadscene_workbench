from __future__ import annotations

from pathlib import Path

import pytest

from cadscene.core.config import (
    apply_cli_overrides,
    load_dataset_config,
    load_pipeline_config,
    resolve_pipeline_references,
    validate_config,
)


def _write_yaml(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_dataset_and_pipeline_config_load(tmp_path: Path) -> None:
    dataset = _write_yaml(tmp_path / "configs/datasets/demo.yaml", "dataset_name: demo\ncad_scale: 1.0\norigin_xy: [0, 0]\n")
    pipeline = _write_yaml(tmp_path / "pipeline.yaml", "pipeline_name: demo_pipe\nstages: {}\n")

    assert load_dataset_config(dataset)["dataset_name"] == "demo"
    assert load_pipeline_config(pipeline)["pipeline_name"] == "demo_pipe"


def test_resolve_dataset_and_run_references(tmp_path: Path) -> None:
    dataset = {"dataset_name": "demo", "trajectory_path": "traj.json", "cad_scale": 1.0, "origin_xy": [1, 2]}
    pipeline = {
        "stages": {
            "alignment": {"enabled": True, "output_subdir": "03_alignment", "inputs": {"trajectory": "${dataset.trajectory_path}"}, "params": {"origin_xy": "${dataset.origin_xy}"}},
            "quality": {"enabled": True, "output_subdir": "04_quality", "inputs": {"alignment": "${run.03_alignment.alignment}"}, "params": {}},
        }
    }

    resolved = resolve_pipeline_references(pipeline, dataset, output_root=tmp_path / "runs", dataset_name="demo", run_id="r1")

    assert resolved["stages"]["alignment"]["inputs"]["trajectory"] == "traj.json"
    assert resolved["stages"]["alignment"]["params"]["origin_xy"] == [1, 2]
    assert resolved["stages"]["quality"]["inputs"]["alignment"].endswith("runs/demo/r1/03_alignment/alignment.json")


def test_resolve_sfm_run_artifacts(tmp_path: Path) -> None:
    pipeline = {
        "stages": {
            "alignment": {
                "enabled": True,
                "output_subdir": "03_alignment",
                "inputs": {"trajectory": "${run.02_sfm.camera_trajectory}"},
            }
        }
    }

    resolved = resolve_pipeline_references(
        pipeline,
        {"dataset_name": "demo"},
        output_root=tmp_path / "runs",
        dataset_name="demo",
        run_id="r1",
    )

    assert resolved["stages"]["alignment"]["inputs"]["trajectory"].endswith(
        "runs/demo/r1/02_sfm/camera_trajectory.json"
    )


def test_cli_overrides_take_precedence() -> None:
    dataset = {"dataset_name": "demo", "cad_scale": 1.0, "origin_xy": [0, 0], "trajectory_path": None}

    out = apply_cli_overrides(dataset, {"cad_scale": 2.0, "trajectory_path": "override.json"})

    assert out["cad_scale"] == 2.0
    assert out["trajectory_path"] == "override.json"


def test_validate_rejects_bad_output_subdir_and_cad_scale() -> None:
    with pytest.raises(ValueError, match="output_subdir"):
        validate_config({"cad_scale": 1.0}, {"stages": {"bad": {"enabled": True, "output_subdir": "../bad"}}})

    with pytest.raises(ValueError, match="cad_scale"):
        validate_config({"cad_scale": 0.0}, {"stages": {}})


def test_missing_required_input_reports_in_dry_run(tmp_path: Path) -> None:
    dataset = {"dataset_name": "demo", "trajectory_path": None, "cad_scale": 1.0, "origin_xy": [0, 0]}
    pipeline = {"stages": {"alignment": {"enabled": True, "output_subdir": "03_alignment", "inputs": {"trajectory": "${dataset.trajectory_path}"}, "params": {}}}}
    resolved = resolve_pipeline_references(pipeline, dataset, output_root=tmp_path, dataset_name="demo", run_id="r1")

    report = validate_config(dataset, resolved, dry_run=True)

    assert report["missing_inputs"][0]["input"] == "trajectory"
