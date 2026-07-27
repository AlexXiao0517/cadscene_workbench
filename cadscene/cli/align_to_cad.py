from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadscene.alignment.aligner import AlignmentConfig, build_alignment_report, run_alignment
from cadscene.core.artifacts import ArtifactManager
from cadscene.core.io import write_csv_utf8_sig, write_json, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 SfM trajectory 与 web keyframes 对齐到 CAD meters。")
    parser.add_argument("--config", default=None, help="pipeline 配置文件路径；Stage 3A 暂只记录，不强制解析。")
    parser.add_argument("--dataset", required=True, help="数据集名称。")
    parser.add_argument("--run-id", required=True, help="run 标识。")
    parser.add_argument("--output-root", default="runs", help="输出根目录，默认 runs。")
    parser.add_argument("--trajectory", required=True, help="SfM camera_trajectory.json。")
    parser.add_argument("--web-camera-track", required=True, help="旧 viewer 下载或人工确认后的 camera track JSON。")
    parser.add_argument("--cad-dir", required=True, help="CAD assets 目录；Stage 3A 用作输入登记。")
    parser.add_argument("--cad-scale", type=float, required=True, help="web cad_world 到 CAD meters 的比例。")
    parser.add_argument("--origin-xy", type=float, nargs=2, required=True, metavar=("X", "Y"), help="web cad_world 原点。")
    parser.add_argument("--fov", type=float, default=70.0, help="默认水平 FOV。")
    parser.add_argument("--fov-from", choices=["trajectory", "config"], default="trajectory", help="FOV 来源。")
    parser.add_argument("--frontend-track-step", type=int, default=10, help="camera_track_pred algorithm_prediction 帧间隔。")
    parser.add_argument("--frame-step", type=int, default=1, help="sfm_camera_path.csv 帧间隔。")
    parser.add_argument("--start-frame", type=int, default=None, help="输出起始帧。")
    parser.add_argument("--end-frame", type=int, default=None, help="输出结束帧。")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = AlignmentConfig(
        cad_scale=args.cad_scale,
        origin_xy=(float(args.origin_xy[0]), float(args.origin_xy[1])),
        fov=args.fov,
        fov_from=args.fov_from,
        frontend_track_step=args.frontend_track_step,
        frame_step=args.frame_step,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
    )
    manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
    stage_dir = manager.stage_dir("alignment", "03_alignment")
    result = run_alignment(
        trajectory_path=args.trajectory,
        web_camera_track_path=args.web_camera_track,
        cad_dir=args.cad_dir,
        config=config,
    )
    alignment_json = stage_dir / "alignment.json"
    sfm_camera_path = stage_dir / "sfm_camera_path.csv"
    camera_track_pred = stage_dir / "camera_track_pred.json"
    correspondences_csv = stage_dir / "keyframe_correspondences.csv"
    report_md = stage_dir / "alignment_report.md"
    write_json(alignment_json, result.alignment_json)
    write_csv_utf8_sig(sfm_camera_path, result.sfm_camera_path_rows)
    write_json(camera_track_pred, result.camera_track_pred)
    write_csv_utf8_sig(correspondences_csv, result.keyframe_correspondences)
    write_text(
        report_md,
        build_alignment_report(
            result.metrics,
            config,
            validation=result.alignment_json["validation"],
        ),
    )
    manager.record_stage(
        stage_name="alignment",
        command=[sys.executable, "-m", "cadscene.cli.align_to_cad", *sys.argv[1:]],
        inputs={
            "config": str(args.config) if args.config else None,
            "trajectory": str(Path(args.trajectory)),
            "web_camera_track": str(Path(args.web_camera_track)),
            "cad_dir": str(Path(args.cad_dir)),
        },
        outputs={
            "alignment_json": str(alignment_json),
            "sfm_camera_path": str(sfm_camera_path),
            "camera_track_pred": str(camera_track_pred),
            "keyframe_correspondences": str(correspondences_csv),
            "alignment_report": str(report_md),
        },
        metrics=result.metrics,
        status="success",
    )
    print(f"alignment outputs written to {stage_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
