from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


RUN_ARTIFACTS = {
    "02_sfm": {
        "camera_trajectory": "camera_trajectory.json",
        "trajectory": "camera_trajectory.json",
        "sparse_points": "sparse_points.ply",
        "sparse_ply": "sparse_points.ply",
        "camera_intrinsics": "camera_intrinsics.json",
        "sfm_stats": "sfm_stats.json",
    },
    "03_alignment": {
        "alignment": "alignment.json",
        "alignment_json": "alignment.json",
        "sfm_camera_path": "sfm_camera_path.csv",
        "camera_track_pred": "camera_track_pred.json",
    },
    "04_quality": {
        "quality_timeline": "quality_timeline.csv",
        "keyframe_suggestions": "keyframe_suggestions.json",
        "camera_track_pred_quality": "camera_track_pred_quality.json",
    },
    "05_viewer_scene": {"sfm_viewer_scene": "sfm_viewer_scene.json"},
    "06_road_surface": {
        "viewer_diagnostics_scene": "viewer_diagnostics_scene.json",
        "sfm_geometry_summary": "sfm_geometry_summary.json",
    },
    "08_render": {
        "overlay_video": "sfm_align_overlay.mp4",
        "render_stats": "render_stats.json",
    },
}

TEMPLATE_RE = re.compile(r"^\$\{([^}]+)\}$")


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_dataset_config(path_or_dataset_name: str | Path) -> dict:
    src = Path(path_or_dataset_name)
    if not src.exists():
        src = Path("configs") / "datasets" / f"{path_or_dataset_name}.yaml"
    data = _read_yaml(src)
    if "dataset_name" not in data:
        data["dataset_name"] = src.stem
    return data


def load_pipeline_config(path: str | Path) -> dict:
    return _read_yaml(Path(path))


def apply_cli_overrides(dataset: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(dict(dataset))
    for key, value in overrides.items():
        if value is not None:
            out[key] = value
    return out


def _lookup_run(output_root: str | Path, dataset_name: str, run_id: str, stage: str, artifact: str) -> str:
    file_name = RUN_ARTIFACTS.get(stage, {}).get(artifact, artifact)
    return (Path(output_root) / dataset_name / run_id / stage / file_name).as_posix()


def _resolve_value(value: Any, dataset: Mapping[str, Any], *, output_root: str | Path, dataset_name: str, run_id: str) -> Any:
    if isinstance(value, dict):
        return {k: _resolve_value(v, dataset, output_root=output_root, dataset_name=dataset_name, run_id=run_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, dataset, output_root=output_root, dataset_name=dataset_name, run_id=run_id) for v in value]
    if not isinstance(value, str):
        return value
    match = TEMPLATE_RE.match(value.strip())
    if not match:
        return value
    ref = match.group(1)
    parts = ref.split(".")
    if len(parts) == 2 and parts[0] == "dataset":
        return dataset.get(parts[1])
    if len(parts) == 3 and parts[0] == "run":
        return _lookup_run(output_root, dataset_name, run_id, parts[1], parts[2])
    raise ValueError(f"unsupported config reference: {value}")


def resolve_pipeline_references(pipeline: Mapping[str, Any], dataset: Mapping[str, Any], *, output_root: str | Path, dataset_name: str, run_id: str) -> dict:
    return _resolve_value(copy.deepcopy(dict(pipeline)), dataset, output_root=output_root, dataset_name=dataset_name, run_id=run_id)


def validate_config(dataset: Mapping[str, Any], pipeline: Mapping[str, Any], *, dry_run: bool = False) -> dict:
    if float(dataset.get("cad_scale", 0) or 0) <= 0:
        raise ValueError("cad_scale must be > 0")
    missing: list[dict] = []
    for stage_name, stage in (pipeline.get("stages") or {}).items():
        if not stage or not stage.get("enabled", True):
            continue
        output_subdir = Path(str(stage.get("output_subdir", "")))
        if output_subdir.is_absolute() or ".." in output_subdir.parts:
            raise ValueError(f"{stage_name}.output_subdir must stay inside run directory")
        for input_name, value in (stage.get("inputs") or {}).items():
            if value in (None, ""):
                missing.append({"stage": stage_name, "input": input_name, "path": value})
                continue
            if isinstance(value, str) and _looks_like_run_artifact(value):
                continue
            if isinstance(value, str) and input_name not in {"quality_timeline", "suggestions"} and not Path(value).exists():
                missing.append({"stage": stage_name, "input": input_name, "path": value})
    if missing and not dry_run:
        first = missing[0]
        raise FileNotFoundError(f"missing required input: {first['stage']}.{first['input']} -> {first['path']}")
    return {"missing_inputs": missing}


def _looks_like_run_artifact(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return any(f"/{stage}/" in normalized for stage in RUN_ARTIFACTS)
