from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadscene.core.artifacts import ArtifactManager
from cadscene.core.io import write_json, write_text
from cadscene.viewer.export_scene import ExportViewerSceneConfig, build_viewer_scene, build_viewer_scene_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出 SfM 点云、双轨迹与建议帧为前端 viewer scene JSON。")
    parser.add_argument("--config", default=None, help="pipeline 配置路径；Stage 3C 暂只记录。")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--sparse-ply", default=None)
    parser.add_argument("--trajectory", default=None)
    parser.add_argument("--alignment", required=True)
    parser.add_argument("--sfm-camera-path", default=None)
    parser.add_argument("--quality-timeline", default=None)
    parser.add_argument("--suggestions", default=None)
    parser.add_argument("--cad-scale", type=float, required=True)
    parser.add_argument("--origin-xy", type=float, nargs=2, required=True, metavar=("X", "Y"))
    parser.add_argument("--max-points", type=int, default=80000)
    parser.add_argument("--point-sample-mode", choices=["voxel", "random", "uniform"], default="voxel")
    parser.add_argument("--voxel-size", type=float, default=0.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
    stage_dir = manager.stage_dir("viewer_scene", "05_viewer_scene")
    config = ExportViewerSceneConfig(
        cad_scale=args.cad_scale,
        origin_xy=(float(args.origin_xy[0]), float(args.origin_xy[1])),
        max_points=args.max_points,
        point_sample_mode=args.point_sample_mode,
        voxel_size=args.voxel_size,
    )
    scene, stats = build_viewer_scene(
        dataset=args.dataset,
        run_id=args.run_id,
        sparse_ply=args.sparse_ply,
        trajectory=args.trajectory,
        alignment=args.alignment,
        sfm_camera_path=args.sfm_camera_path,
        quality_timeline=args.quality_timeline,
        suggestions=args.suggestions,
        config=config,
    )
    scene_path = stage_dir / "sfm_viewer_scene.json"
    stats_path = stage_dir / "sfm_viewer_scene_stats.json"
    report_path = stage_dir / "sfm_viewer_scene_report.md"
    write_json(scene_path, scene)
    write_json(stats_path, stats)
    inputs = {
        "sparse_ply": args.sparse_ply or "",
        "trajectory": args.trajectory or "",
        "alignment": args.alignment,
        "sfm_camera_path": args.sfm_camera_path or "",
        "quality_timeline": args.quality_timeline or "",
        "suggestions": args.suggestions or "",
    }
    write_text(report_path, build_viewer_scene_report(inputs, stats))
    manager.record_stage(
        stage_name="viewer_scene",
        command=[sys.executable, "-m", "cadscene.cli.export_viewer_scene", *sys.argv[1:]],
        inputs={
            "config": args.config,
            **inputs,
            "cad_scale": args.cad_scale,
            "origin_xy": args.origin_xy,
        },
        outputs={
            "sfm_viewer_scene": str(scene_path),
            "sfm_viewer_scene_stats": str(stats_path),
            "sfm_viewer_scene_report": str(report_path),
        },
        metrics={
            "point_count_exported": stats["point_count_exported"],
            "global_track_count": stats["global_track_count"],
            "anchored_track_count": stats["anchored_track_count"],
            "suggestion_count": stats["suggestion_count"],
        },
        status="success",
    )
    print(f"viewer scene outputs written to {stage_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
