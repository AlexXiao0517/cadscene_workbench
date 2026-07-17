from __future__ import annotations

import argparse
import sys

import numpy as np

from cadscene.core.artifacts import ArtifactManager
from cadscene.core.io import write_csv_utf8_sig, write_json, write_text
from cadscene.diagnostics.pose_residual import camera_z_profile, keyframe_pose_residuals, pose_compensation_warning
from cadscene.diagnostics.road_surface import (
    extract_road_points_corridor,
    fit_global_plane,
    flat_plane_error,
    load_centerline,
    profile_surface_error,
    project_to_centerline,
    road_surface_profile,
    sfm_surface_quality,
    summarize_geometry,
    transform_pointcloud_to_cad,
    viewer_diagnostics_scene,
    write_ply_cadworld,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SfM road surface / geometry diagnostics（只读，不改 CAD 或 pose）。")
    parser.add_argument("--config", default=None)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--sparse-ply", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--alignment", required=True)
    parser.add_argument("--sfm-camera-path", required=True)
    parser.add_argument("--web-camera-track", default=None)
    parser.add_argument("--cad-dir", required=True)
    parser.add_argument("--cad-scale", type=float, required=True)
    parser.add_argument("--origin-xy", type=float, nargs=2, required=True)
    parser.add_argument("--max-points", type=int, default=120000)
    parser.add_argument("--point-sample-mode", choices=["voxel", "random", "uniform"], default="voxel")
    parser.add_argument("--voxel-size", type=float, default=0.5)
    parser.add_argument("--station-bin-m", type=float, default=20.0)
    parser.add_argument("--road-corridor-width", type=float, default=15.0)
    parser.add_argument("--seg-dir", default=None)
    parser.add_argument("--export-viewer-scene", action="store_true")
    return parser


def _report(inputs, summary, warnings) -> str:
    c = summary["conclusions"]
    return "\n".join(
        [
            "# SfM 道路表面 / 几何诊断报告",
            "",
            "## 输入文件",
            "",
            *[f"- {k}: `{v}`" for k, v in inputs.items()],
            "",
            "## 诊断结论",
            "",
            f"- road_flatness_status: {c['road_flatness_status']}",
            f"- road_surface_reliability: {c['road_surface_reliability']}",
            f"- cad_flat_plane_assumption: {c['cad_flat_plane_assumption']}",
            f"- sfm_as_alignment_reference: {c['sfm_as_alignment_reference']}",
            f"- next_step_recommendation: {c['next_step_recommendation']}",
            f"- pose_compensation_warning: {c['pose_compensation_warning']}",
            "",
            "## 点云与道路支撑",
            "",
            f"- road_point_count: {summary['road_point_count']}",
            f"- road_point_ratio: {summary['road_point_ratio']:.3f}",
            "",
            "## 高度模型误差",
            "",
            f"- flat z=0 rmse: {summary['flat_plane'].get('rmse', 0):.3f}",
            f"- global plane rmse: {summary['global_plane'].get('rmse', 0):.3f}",
            f"- station profile rmse: {summary['station_profile'].get('rmse', 0):.3f}",
            "",
            "## Warning",
            "",
            *[f"- {w}" for w in warnings],
            "",
            "## 声明",
            "",
            "本阶段只读诊断，不修改 CAD 或相机轨迹；未输入外部地图高程时，SfM z 只能解释为当前 SfM-CAD 坐标系下相对 CAD z=0 的偏差。",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
    stage_dir = manager.stage_dir("road_surface", "06_road_surface")
    warnings = ["semantic road point extraction unavailable in v0.1."]
    points_cad, colors, original_count = transform_pointcloud_to_cad(args.sparse_ply, args.alignment, args.max_points, args.point_sample_mode, args.voxel_size)
    centerline = load_centerline(
        args.cad_dir,
        origin_xy=args.origin_xy,
        cad_scale=args.cad_scale,
    )
    if centerline is None:
        warnings.append("cad_dir 中没有可用 road centerline，road points insufficient。")
        station = np.zeros(len(points_cad))
        road_mask = np.zeros(len(points_cad), dtype=bool)
    else:
        station, lateral, _nearest = project_to_centerline(points_cad[:, :2], centerline)
        road_mask = extract_road_points_corridor(points_cad, lateral, args.road_corridor_width)
    road_pts = points_cad[road_mask]
    road_station = station[road_mask]
    road_colors = colors[road_mask] if colors is not None and len(colors) == len(points_cad) else None
    profile_rows = road_surface_profile(road_pts, road_station, args.station_bin_m)
    flat = flat_plane_error(road_pts[:, 2] if len(road_pts) else [])
    plane = fit_global_plane(road_pts)
    profile = profile_surface_error(road_pts, road_station, args.station_bin_m)
    residuals = []
    if args.web_camera_track:
        try:
            residuals = keyframe_pose_residuals(args.web_camera_track, args.trajectory, args.alignment, args.sfm_camera_path, args.cad_scale, args.origin_xy)
        except Exception as exc:
            warnings.append(f"keyframe pose residual unavailable: {exc}")
    pose_warn = pose_compensation_warning(residuals, profile_rows)
    summary = summarize_geometry(road_point_count=len(road_pts), road_point_ratio=float(len(road_pts) / max(1, len(points_cad))), flat=flat, plane=plane, profile=profile, pose_warning=pose_warn)
    point_stats = {"point_count_original": original_count, "point_count_sampled": int(len(points_cad)), "road_point_count": int(len(road_pts)), "road_point_ratio": float(len(road_pts) / max(1, len(points_cad))), "has_rgb": colors is not None}
    z_profile = []
    if args.web_camera_track:
        try:
            z_profile = camera_z_profile(args.trajectory, args.alignment, args.sfm_camera_path, args.web_camera_track, args.cad_scale, args.origin_xy, profile_rows)
        except Exception as exc:
            warnings.append(f"camera z profile unavailable: {exc}")
    flatness_rows = [{"model": "flat_z0", **flat}, {"model": "global_plane", **plane}, {"model": "station_profile", **profile}]
    quality_rows = sfm_surface_quality(profile_rows)
    outputs = {
        "sfm_geometry_report": stage_dir / "sfm_geometry_report.md",
        "sfm_geometry_summary": stage_dir / "sfm_geometry_summary.json",
        "point_cloud_stats": stage_dir / "point_cloud_stats.json",
        "road_surface_profile": stage_dir / "road_surface_profile.csv",
        "road_points_cadworld": stage_dir / "road_points_cadworld.ply",
        "keyframe_pose_residuals": stage_dir / "keyframe_pose_residuals.csv",
        "cad_flatness_error": stage_dir / "cad_flatness_error.csv",
        "camera_z_profile": stage_dir / "camera_z_profile.csv",
        "sfm_surface_quality": stage_dir / "sfm_surface_quality.csv",
    }
    write_json(outputs["sfm_geometry_summary"], summary)
    write_json(outputs["point_cloud_stats"], point_stats)
    write_csv_utf8_sig(outputs["road_surface_profile"], profile_rows)
    write_ply_cadworld(outputs["road_points_cadworld"], road_pts, road_colors)
    write_csv_utf8_sig(outputs["keyframe_pose_residuals"], residuals)
    write_csv_utf8_sig(outputs["cad_flatness_error"], flatness_rows)
    write_csv_utf8_sig(outputs["camera_z_profile"], z_profile)
    write_csv_utf8_sig(outputs["sfm_surface_quality"], quality_rows)
    inputs = {"sparse_ply": args.sparse_ply, "trajectory": args.trajectory, "alignment": args.alignment, "sfm_camera_path": args.sfm_camera_path, "web_camera_track": args.web_camera_track or "", "cad_dir": args.cad_dir, "seg_dir": args.seg_dir or ""}
    write_text(outputs["sfm_geometry_report"], _report(inputs, summary, warnings))
    if args.export_viewer_scene:
        outputs["viewer_diagnostics_scene"] = stage_dir / "viewer_diagnostics_scene.json"
        write_json(outputs["viewer_diagnostics_scene"], viewer_diagnostics_scene(road_pts, profile_rows, residuals, args.origin_xy, args.cad_scale, warnings))
    manager.record_stage(
        stage_name="road_surface",
        command=[sys.executable, "-m", "cadscene.cli.analyze_road_surface", *sys.argv[1:]],
        inputs={**inputs, "config": args.config, "cad_scale": args.cad_scale, "origin_xy": args.origin_xy},
        outputs={key: str(value) for key, value in outputs.items()},
        metrics={"road_point_count": len(road_pts), "road_point_ratio": point_stats["road_point_ratio"]},
        status="success",
    )
    print(f"road surface diagnostics written to {stage_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
