from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

from cadscene.cli import align_to_cad, analyze_road_surface, evaluate_quality, export_viewer_scene, render_overlay, run_sfm
from cadscene.cli._progress import write_progress_sidecar
from cadscene.cad.loader import RoadCenterlineCapability, detect_road_centerline
from cadscene.core.artifacts import ArtifactManager
from cadscene.core.config import apply_cli_overrides, load_dataset_config, load_pipeline_config, resolve_pipeline_references, validate_config
from cadscene.core.io import ensure_dir, write_json, write_text
from cadscene.workflow.job_status import JobStatusStore

STAGE_ORDER = ["sfm", "alignment", "quality", "viewer_scene", "road_surface", "render"]
OPTIONAL_STAGE_INPUTS = {"viewer_scene": {"quality_timeline", "suggestions"}}
ROAD_SURFACE_SKIP_MESSAGE = "当前 CAD 未检测到道路中心线，已跳过道路表面诊断"


def _is_quality_progress_run(requested: list[str], progress_file: Path | None) -> bool:
    return bool(
        progress_file is not None
        and "quality" in requested
        and set(requested).issubset({"quality", "viewer_scene", "road_surface"})
    )


def _write_quality_progress(
    progress_file: Path | None,
    *,
    enabled: bool,
    stage: str,
    message: str,
    fraction: float,
) -> None:
    if enabled and progress_file is not None:
        write_progress_sidecar(progress_file, stage, message, fraction)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 cadscene v0.1 SfM-CAD pipeline。")
    parser.add_argument("--config", default="configs/pipelines/sfm_overlay_existing_sfm.yaml")
    parser.add_argument("--dataset", default="hygs_1min")
    parser.add_argument("--run-id", default="demo")
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stages", default=None)
    parser.add_argument("--skip-render", action="store_true")
    parser.add_argument("--skip-road-surface", action="store_true")
    parser.add_argument("--skip-viewer-scene", action="store_true")
    parser.add_argument("--trajectory", dest="trajectory_path")
    parser.add_argument("--sparse-ply", dest="sparse_ply_path")
    parser.add_argument("--web-camera-track", dest="default_track")
    parser.add_argument("--video", dest="video_path")
    parser.add_argument("--cad-dir", dest="cad_dir")
    parser.add_argument("--cad-scale", dest="cad_scale", type=float)
    parser.add_argument("--origin-xy", dest="origin_xy", type=float, nargs=2)
    parser.add_argument("--progress-file", type=Path)
    return parser


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _append_value(argv: list[str], name: str, value: Any) -> None:
    if value is None:
        return
    flag = _flag(name)
    if isinstance(value, bool):
        if value:
            argv.append(flag)
        return
    argv.append(flag)
    if isinstance(value, (list, tuple)):
        argv.extend(str(v) for v in value)
    else:
        argv.append(str(value))


def _base_args(args: argparse.Namespace) -> list[str]:
    return ["--dataset", args.dataset_name, "--run-id", args.run_id, "--output-root", args.output_root, "--config", args.config]


def _stage_command(stage_name: str, stage: Mapping[str, Any], args: argparse.Namespace) -> tuple[str, list[str], Any]:
    inputs = dict(stage.get("inputs") or {})
    params = dict(stage.get("params") or {})
    argv = _base_args(args)
    if stage_name == "sfm":
        module, func = "run_sfm", run_sfm.main
        mapping = {"video": "video", "seg_dir": "seg_dir"}
    elif stage_name == "alignment":
        module, func = "align_to_cad", align_to_cad.main
        mapping = {"trajectory": "trajectory", "web_camera_track": "web_camera_track", "cad_dir": "cad_dir"}
    elif stage_name == "quality":
        module, func = "evaluate_quality", evaluate_quality.main
        mapping = {
            "sfm_camera_path": "sfm_camera_path",
            "alignment": "alignment",
            "web_camera_track": "web_camera_track",
            "trajectory": "trajectory",
            "cad_dir": "cad_dir",
        }
    elif stage_name == "viewer_scene":
        module, func = "export_viewer_scene", export_viewer_scene.main
        mapping = {
            "sparse_ply": "sparse_ply",
            "trajectory": "trajectory",
            "alignment": "alignment",
            "sfm_camera_path": "sfm_camera_path",
            "quality_timeline": "quality_timeline",
            "suggestions": "suggestions",
        }
    elif stage_name == "road_surface":
        module, func = "analyze_road_surface", analyze_road_surface.main
        mapping = {
            "sparse_ply": "sparse_ply",
            "trajectory": "trajectory",
            "alignment": "alignment",
            "sfm_camera_path": "sfm_camera_path",
            "web_camera_track": "web_camera_track",
            "cad_dir": "cad_dir",
        }
    elif stage_name == "render":
        module, func = "render_overlay", render_overlay.main
        mapping = {"video": "video", "cad_dir": "cad_dir", "sfm_camera_path": "sfm_camera_path"}
    else:
        raise ValueError(f"unsupported stage: {stage_name}")
    optional_inputs = OPTIONAL_STAGE_INPUTS.get(stage_name, set())
    for source, target in mapping.items():
        value = inputs.get(source)
        if source in optional_inputs and value and not Path(str(value)).exists():
            continue
        _append_value(argv, target, value)
    for key, value in params.items():
        _append_value(argv, key, value)
    return module, argv, func


def _selected_stages(args: argparse.Namespace, pipeline: Mapping[str, Any]) -> list[str]:
    wanted = [item.strip() for item in args.stages.split(",") if item.strip()] if args.stages else STAGE_ORDER[:]
    for skip, name in ((args.skip_render, "render"), (args.skip_road_surface, "road_surface"), (args.skip_viewer_scene, "viewer_scene")):
        if skip and name in wanted:
            wanted.remove(name)
    stages = pipeline.get("stages") or {}
    return [name for name in wanted if name in stages and stages[name].get("enabled", True)]


def _road_surface_capability(
    selected: list[str], resolved: Mapping[str, Any]
) -> RoadCenterlineCapability | None:
    if "road_surface" not in selected:
        return None
    stage = (resolved.get("stages") or {}).get("road_surface") or {}
    cad_dir = (stage.get("inputs") or {}).get("cad_dir")
    if not cad_dir or not Path(str(cad_dir)).exists():
        return None
    return detect_road_centerline(cad_dir)


def _record_road_surface_skip(
    manager: ArtifactManager,
    capability: RoadCenterlineCapability,
    command: list[str],
) -> None:
    manager.record_stage(
        stage_name="road_surface",
        command=command,
        inputs={},
        outputs={},
        metrics={
            "has_road_centerline": capability.has_road_centerline,
            "road_centerline_source": capability.source,
            "reason": ROAD_SURFACE_SKIP_MESSAGE,
        },
        status="skipped",
    )


def _write_pipeline_artifacts(manager: ArtifactManager, dataset: Mapping[str, Any], pipeline: Mapping[str, Any], overrides: Mapping[str, Any], commands: list[str], viewer_url: str, summary: str) -> None:
    inputs_dir = ensure_dir(manager.run_dir / "00_inputs")
    reports_dir = ensure_dir(manager.run_dir / "reports")
    logs_dir = ensure_dir(manager.run_dir / "logs")
    write_json(inputs_dir / "dataset.resolved.json", dict(dataset))
    write_json(inputs_dir / "pipeline.resolved.json", dict(pipeline))
    write_json(inputs_dir / "overrides.json", dict(overrides))
    write_text(logs_dir / "commands.txt", "\n".join(commands) + ("\n" if commands else ""))
    write_text(reports_dir / "viewer_url.txt", viewer_url + "\n")
    write_text(reports_dir / "run_summary.md", summary)


def _summary(dataset_name: str, run_id: str, stages: list[str], resolved: Mapping[str, Any], statuses: Mapping[str, str], viewer_url: str, warnings: list[str]) -> str:
    lines = ["# Pipeline 运行摘要", "", f"- dataset: {dataset_name}", f"- run_id: {run_id}", f"- enabled stages: {', '.join(stages)}", f"- viewer URL: {viewer_url}", "", "## Stage 状态", ""]
    for name in stages:
        stage = resolved.get("stages", {}).get(name, {})
        lines.extend([f"### {name}", f"- status: {statuses.get(name, 'pending')}", f"- inputs: {stage.get('inputs', {})}", f"- output_subdir: {stage.get('output_subdir', '')}", ""])
    lines.extend(["## Warnings", "", *(f"- {w}" for w in (warnings or ["无"])), "", "## v0.1 限制", "", "- segmentation / DINOv3 尚未迁移，SfM 默认无语义 mask。", "- 当前不做 semantic refine。", "- 当前不做 CAD-on-tilted-plane apply。", ""])
    return "\n".join(lines)


def _load_dataset_for_run(dataset_arg: str) -> dict[str, Any]:
    try:
        return load_dataset_config(dataset_arg)
    except FileNotFoundError:
        candidate = Path(dataset_arg)
        if candidate.suffix.lower() in {".yaml", ".yml"} or len(candidate.parts) > 1:
            raise
        # 前端上传生成的 dataset 由 CLI overrides 提供完整路径与坐标参数，
        # 不要求为每次本地导入额外创建 configs/datasets/<name>.yaml。
        return {"dataset_name": dataset_arg}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raw_dataset = _load_dataset_for_run(args.dataset)
        overrides = {
            "trajectory_path": args.trajectory_path,
            "sparse_ply_path": args.sparse_ply_path,
            "default_track": args.default_track,
            "video_path": args.video_path,
            "cad_dir": args.cad_dir,
            "cad_scale": args.cad_scale,
            "origin_xy": args.origin_xy,
        }
        dataset = apply_cli_overrides(raw_dataset, overrides)
        args.dataset_name = str(dataset.get("dataset_name") or args.dataset)
        pipeline = load_pipeline_config(args.config)
        resolved = resolve_pipeline_references(pipeline, dataset, output_root=args.output_root, dataset_name=args.dataset_name, run_id=args.run_id)
        render_stage = (resolved.get("stages") or {}).get("render")
        if render_stage is not None:
            render_params = render_stage.setdefault("params", {})
            render_params.setdefault("cad_scale", dataset.get("cad_scale"))
            render_params.setdefault("origin_xy", dataset.get("origin_xy"))
            render_params.setdefault(
                "progress_file",
                None if args.progress_file is None else str(args.progress_file),
            )
        requested = _selected_stages(args, resolved)
        quality_progress_run = _is_quality_progress_run(requested, args.progress_file)
        if quality_progress_run:
            quality_params = resolved["stages"]["quality"].setdefault("params", {})
            quality_params["progress_file"] = str(args.progress_file)
            quality_params["progress_start"] = 0.10
            quality_params["progress_evaluation_end"] = 0.45
            quality_params["progress_end"] = 0.55
        resolved["stages"] = {
            name: stage for name, stage in resolved.get("stages", {}).items() if name in requested
        }
        centerline = _road_surface_capability(requested, resolved)
        selected = list(requested)
        road_surface_skipped = centerline is not None and not centerline.has_road_centerline
        if road_surface_skipped:
            selected.remove("road_surface")
            resolved["stages"]["road_surface"]["skip_reason"] = ROAD_SURFACE_SKIP_MESSAGE
        execution_config = {
            **resolved,
            "stages": {name: resolved["stages"][name] for name in selected},
        }
        validation = validate_config(dataset, execution_config, dry_run=args.dry_run)
        manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset_name, run_id=args.run_id)
        plans: list[tuple[str, list[str], Any]] = []
        commands: list[str] = []
        for name in selected:
            module, stage_argv, func = _stage_command(name, execution_config["stages"][name], args)
            commands.append(f"python -m cadscene.cli.{module} " + " ".join(stage_argv))
            plans.append((name, stage_argv, func))
        viewer_url = f"http://127.0.0.1:8300/apps/web_camera_viewer/?dataset={args.dataset_name}&runId={args.run_id}"
        statuses: dict[str, str] = {
            name: (
                "skipped"
                if name == "road_surface" and road_surface_skipped
                else ("dry_run" if args.dry_run else "pending")
            )
            for name in requested
        }
        warnings = [f"missing input in dry-run: {item['stage']}.{item['input']} -> {item['path']}" for item in validation["missing_inputs"]]
        if road_surface_skipped and centerline is not None:
            warnings.append(ROAD_SURFACE_SKIP_MESSAGE)
            _record_road_surface_skip(
                manager,
                centerline,
                [sys.executable, "-m", "cadscene.cli.run_pipeline", *(argv or sys.argv[1:])],
            )
        if args.dry_run:
            summary = _summary(args.dataset_name, args.run_id, requested, resolved, statuses, viewer_url, warnings)
            _write_pipeline_artifacts(manager, dataset, resolved, {k: v for k, v in overrides.items() if v is not None}, commands, viewer_url, summary)
            manager.record_stage(stage_name="dry_run", command=[sys.executable, "-m", "cadscene.cli.run_pipeline", *(argv or sys.argv[1:])], inputs={"config": args.config, "dataset": args.dataset}, outputs={"run_summary": str(manager.run_dir / "reports" / "run_summary.md")}, metrics={"stage_count": len(selected), "missing_input_count": len(validation["missing_inputs"])}, status="dry_run")
            print(summary)
            return 0
        _write_quality_progress(
            args.progress_file,
            enabled=quality_progress_run,
            stage="quality_prepare",
            message="正在校验质量检测输入",
            fraction=0.08,
        )
        for name, stage_argv, func in plans:
            if name == "viewer_scene":
                _write_quality_progress(
                    args.progress_file,
                    enabled=quality_progress_run,
                    stage="viewer_scene",
                    message="正在生成工作台质量场景",
                    fraction=0.60,
                )
            elif name == "road_surface":
                _write_quality_progress(
                    args.progress_file,
                    enabled=quality_progress_run,
                    stage="road_surface",
                    message="正在分析道路表面",
                    fraction=0.80,
                )
            rc = int(func(stage_argv) or 0)
            statuses[name] = "success" if rc == 0 else "failed"
            if rc != 0:
                raise RuntimeError(f"stage failed: {name}")
            if name == "viewer_scene":
                _write_quality_progress(
                    args.progress_file,
                    enabled=quality_progress_run,
                    stage="viewer_scene",
                    message="工作台质量场景已生成",
                    fraction=0.75,
                )
            elif name == "road_surface":
                _write_quality_progress(
                    args.progress_file,
                    enabled=quality_progress_run,
                    stage="road_surface",
                    message="道路表面诊断已完成，正在校验发布结果",
                    fraction=0.95,
                )
        if road_surface_skipped:
            _write_quality_progress(
                args.progress_file,
                enabled=quality_progress_run,
                stage="road_surface_skipped",
                message=f"{ROAD_SURFACE_SKIP_MESSAGE}；正在校验发布结果",
                fraction=0.95,
            )
        summary = _summary(args.dataset_name, args.run_id, requested, resolved, statuses, viewer_url, warnings)
        _write_pipeline_artifacts(manager, dataset, resolved, {k: v for k, v in overrides.items() if v is not None}, commands, viewer_url, summary)
        if road_surface_skipped:
            skipped_status = "running" if quality_progress_run else "success"
            skipped_progress = 0.95 if quality_progress_run else 1.0
            JobStatusStore(
                manager.run_dir / "job_status.json", run_id=args.run_id
            ).update_stage(
                "quality",
                status=skipped_status,
                progress=skipped_progress,
                message=ROAD_SURFACE_SKIP_MESSAGE,
                operation="quality",
            )
        print(viewer_url)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
