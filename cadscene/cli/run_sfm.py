from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cadscene.core.artifacts import ArtifactManager
from cadscene.sfm.reconstruction import (
    ReconstructionConfig,
    export_mock_reconstruction,
    run_reconstruction,
    write_reconstruction_outputs,
)
from cadscene.workflow.job_status import JobStatusStore


def _configure_utf8_stdio() -> None:
    """统一后台 SfM 日志编码，避免 Windows 中文输出写成 GBK。"""
    if os.environ.get("CADSCENE_WORKFLOW_LOG_ENCODING", "").lower() != "utf-8":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从原始视频运行 SfM 并导出轨迹与稀疏点云。")
    parser.add_argument("--config", default=None)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--video")
    parser.add_argument("--seg-dir")
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--num-frames", type=int, default=0)
    parser.add_argument("--frame-step", type=int, default=5)
    parser.add_argument("--max-image-size", type=int, default=2048)
    parser.add_argument("--max-num-features", type=int, default=12000)
    parser.add_argument("--camera-model", default="OPENCV")
    parser.add_argument("--sequential-overlap", type=int, default=15)
    parser.add_argument("--init-min-tri-angle", type=float, default=2.0)
    parser.add_argument("--min-reg-images", type=int, default=10)
    parser.add_argument("--reuse-database", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--colmap-exe")
    parser.add_argument("--backend", choices=("auto", "pycolmap", "colmap_cli"), default="pycolmap")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="cpu")
    parser.add_argument("--gpu-index", default="0")
    parser.add_argument("--no-cpu-fallback", action="store_true")
    mask = parser.add_mutually_exclusive_group()
    mask.add_argument("--use-mask", dest="use_mask", action="store_true")
    mask.add_argument("--no-mask", dest="use_mask", action="store_false")
    parser.set_defaults(use_mask=False)
    parser.add_argument("--mock-reconstruction", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
    stage_dir = manager.stage_dir("sfm", "02_sfm")
    status_store = JobStatusStore(
        Path(args.output_root) / args.dataset / args.run_id / "job_status.json",
        run_id=args.run_id,
    )

    def update_progress(substage: str, progress: float, message: str) -> None:
        status_store.update_stage(
            "sfm",
            status="running",
            progress=progress,
            message=message,
            operation="sfm",
        )
    config = ReconstructionConfig(
        start_frame=args.start_frame,
        num_frames=args.num_frames,
        frame_step=args.frame_step,
        max_image_size=args.max_image_size,
        max_num_features=args.max_num_features,
        camera_model=args.camera_model,
        sequential_overlap=args.sequential_overlap,
        init_min_tri_angle=args.init_min_tri_angle,
        min_reg_images=args.min_reg_images,
        reuse_database=args.reuse_database,
        export_only=args.export_only,
        use_mask=args.use_mask,
        colmap_exe=args.colmap_exe,
        backend=args.backend,
        device=args.device,
        gpu_index=args.gpu_index,
        no_cpu_fallback=args.no_cpu_fallback,
    )
    command = [sys.executable, "-m", "cadscene.cli.run_sfm", *(argv or sys.argv[1:])]
    inputs = {
        "config": args.config,
        "video": args.video,
        "seg_dir": args.seg_dir,
        "mock_reconstruction": args.mock_reconstruction,
        "backend": args.backend,
        "device": args.device,
        "gpu_index": args.gpu_index,
        "colmap_exe": args.colmap_exe,
    }
    try:
        if args.mock_reconstruction:
            result = export_mock_reconstruction(args.mock_reconstruction, config)
        else:
            if not args.video:
                raise FileNotFoundError("video does not exist: no --video path was provided")
            if not Path(args.video).exists():
                raise FileNotFoundError(f"video does not exist: {args.video}")
            result = run_reconstruction(
                video_path=args.video,
                output_dir=stage_dir,
                config=config,
                seg_dir=args.seg_dir,
                progress_callback=update_progress,
            )
        outputs = write_reconstruction_outputs(stage_dir, result, video_path=args.video)
        manager.record_stage(
            stage_name="sfm",
            command=command,
            inputs=inputs,
            outputs=outputs,
            metrics=result.stats,
            status="success",
        )
        print(f"SfM outputs written to {stage_dir}")
        return 0
    except Exception as exc:
        report_path = stage_dir / "sfm_report.md"
        report_path.write_text(
            "# SfM 重建失败\n\n"
            f"- 错误：{exc}\n"
            "- 本阶段未修改其他 pipeline 产物。\n",
            encoding="utf-8",
        )
        manager.record_stage(
            stage_name="sfm",
            command=command,
            inputs=inputs,
            outputs={"sfm_report": report_path},
            metrics={"error": str(exc)},
            status="failed",
        )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
