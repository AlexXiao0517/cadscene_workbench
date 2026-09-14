from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadscene.cli._progress import write_progress_sidecar
from cadscene.core.artifacts import ArtifactManager
from cadscene.rendering.calibrated_overlay import (
    CalibratedRenderConfig,
    render_calibrated_overlay_video,
)
from cadscene.rendering.overlay import RenderOverlayConfig, render_overlay_video, write_render_outputs
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render CAD overlay video from aligned SfM camera path.")
    parser.add_argument("--config")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--video", required=True)
    parser.add_argument("--cad-dir", required=True)
    parser.add_argument("--cad-scale", type=float)
    parser.add_argument("--origin-xy", type=float, nargs=2)
    parser.add_argument("--sfm-camera-path", required=True)
    parser.add_argument("--camera-calibration")
    parser.add_argument("--terrain-context")
    parser.add_argument("--terrain-controls")
    parser.add_argument(
        "--output-resolution",
        choices=("720p", "1080p", "source", "4k"),
        default="1080p",
    )
    parser.add_argument("--ffmpeg")
    parser.add_argument("--debug-scale", type=float, default=1.0)
    parser.add_argument("--overlay-linewidth", type=int, default=None)
    parser.add_argument("--overlay-alpha", type=float, default=None)
    parser.add_argument("--faded-overlay", action="store_true")
    parser.add_argument("--max-distance-m", type=float, default=None)
    parser.add_argument("--fade-start-m", type=float, default=None)
    parser.add_argument(
        "--cad-region-bounds",
        type=float,
        nargs=4,
        default=None,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help=(
            "Fixed CAD construction region bounds in CAD-local meters after "
            "origin/scale (not longitude/latitude or raw CAD world coordinates)."
        ),
    )
    parser.add_argument("--cad-region-margin-m", type=float, default=100.0)
    parser.add_argument("--cad-region-lookahead-m", type=float, default=1000.0)
    parser.add_argument("--start-frame", type=int)
    parser.add_argument("--end-frame", type=int)
    parser.add_argument("--sample-every", type=int, default=1)
    parser.add_argument("--write-sample-frames", action="store_true")
    parser.add_argument("--progress-file", type=Path)
    return parser


def _validate_inputs(args: argparse.Namespace) -> None:
    checks = {
        "video": Path(args.video),
        "cad_dir": Path(args.cad_dir),
        "sfm_camera_path": Path(args.sfm_camera_path),
    }
    for name, path in checks.items():
        if not path.exists():
            raise FileNotFoundError(f"{name} not found: {path}")
    calibrated = (
        args.camera_calibration,
        args.terrain_context,
        args.terrain_controls,
    )
    if any(calibrated) and not all(calibrated):
        raise ValueError(
            "camera_calibration, terrain_context and terrain_controls must be provided together"
        )
    for name in ("camera_calibration", "terrain_context", "terrain_controls"):
        value = getattr(args, name)
        if value and not Path(value).is_file():
            raise FileNotFoundError(f"{name} not found: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    defaults = (2, 0.92, None, None) if args.camera_calibration else (3, 0.88, 900.0, 250.0)
    for name, value in zip(("overlay_linewidth", "overlay_alpha", "max_distance_m", "fade_start_m"), defaults):
        if getattr(args, name) is None:
            setattr(args, name, value)
    try:
        _validate_inputs(args)
        artifacts = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
        stage_dir = artifacts.stage_dir("render", "08_render")
        output_video = stage_dir / "sfm_align_overlay.mp4"
        progress_callback = None
        if args.progress_file is not None:
            write_progress_sidecar(
                args.progress_file, "preparing_render", "preparing render", 0.0
            )
            progress_callback = lambda stage, message, fraction: (
                write_progress_sidecar(
                    args.progress_file, stage, message, float(fraction) * 0.95
                )
            )
        if args.camera_calibration:
            config = CalibratedRenderConfig(
                video_path=args.video,
                cad_dir=args.cad_dir,
                camera_path=args.sfm_camera_path,
                camera_calibration=args.camera_calibration,
                terrain_context=args.terrain_context,
                terrain_controls=args.terrain_controls,
                output_video=output_video,
                cad_scale=float(args.cad_scale or 1.0),
                origin_xy=tuple(args.origin_xy or (0.0, 0.0)),
                output_resolution=args.output_resolution,
                overlay_linewidth=args.overlay_linewidth,
                overlay_alpha=args.overlay_alpha,
                max_distance_m=args.max_distance_m,
                fade_start_m=args.fade_start_m,
                cad_region_bounds=(
                    tuple(args.cad_region_bounds)
                    if args.cad_region_bounds is not None
                    else None
                ),
                cad_region_margin_m=args.cad_region_margin_m,
                cad_region_lookahead_m=args.cad_region_lookahead_m,
                output_fps=30.0,
                ffmpeg_executable=str(resolve_ffmpeg_executable(args.ffmpeg)),
            )
            result = render_calibrated_overlay_video(
                config, progress_callback=progress_callback
            )
        else:
            config = RenderOverlayConfig(
                video_path=args.video,
                cad_dir=args.cad_dir,
                sfm_camera_path=args.sfm_camera_path,
                output_video=output_video,
                cad_scale=args.cad_scale,
                origin_xy=tuple(args.origin_xy) if args.origin_xy else None,
                debug_scale=args.debug_scale,
                overlay_linewidth=args.overlay_linewidth,
                overlay_alpha=args.overlay_alpha,
                faded_overlay=args.faded_overlay,
                max_distance_m=args.max_distance_m,
                fade_start_m=args.fade_start_m,
                start_frame=args.start_frame,
                end_frame=args.end_frame,
                sample_every=args.sample_every,
                write_sample_frames=args.write_sample_frames,
                sample_frames_dir=stage_dir / "sample_frames",
            )
            result = render_overlay_video(config, progress_callback=progress_callback)
        inputs = {
            "config": args.config,
            "video": args.video,
            "cad_dir": args.cad_dir,
            "cad_scale": args.cad_scale,
            "origin_xy": args.origin_xy,
            "sfm_camera_path": args.sfm_camera_path,
            "camera_calibration": args.camera_calibration,
            "terrain_context": args.terrain_context,
            "terrain_controls": args.terrain_controls,
            "output_resolution": args.output_resolution,
        }
        write_render_outputs(stage_dir, result, inputs=inputs)
        artifacts.record_stage(
            stage_name="render",
            command=[sys.executable, "-m", "cadscene.cli.render_overlay", *(argv or sys.argv[1:])],
            inputs=inputs,
            outputs={
                "output_video": output_video,
                "render_stats": stage_dir / "render_stats.json",
                "render_report": stage_dir / "render_report.md",
            },
            metrics=result.stats,
            status="success",
        )
        print(output_video)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
