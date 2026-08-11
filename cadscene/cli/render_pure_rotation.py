from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cadscene.core.artifacts import ArtifactManager
from cadscene.pure_rotation.rendering import write_camera_path_csv
from cadscene.rendering.overlay import RenderOverlayConfig, render_overlay_video, write_render_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render CAD overlay from a fixed-center Pure-Rotation track.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--video", required=True)
    parser.add_argument("--cad-dir", required=True)
    parser.add_argument("--track", required=True)
    parser.add_argument("--cad-scale", type=float)
    parser.add_argument("--origin-xy", type=float, nargs=2)
    parser.add_argument("--overlay-linewidth", type=int, default=3)
    parser.add_argument("--overlay-alpha", type=float, default=0.88)
    parser.add_argument("--faded-overlay", action="store_true")
    distance_group = parser.add_mutually_exclusive_group()
    distance_group.add_argument("--max-distance-m", type=float, default=900.0)
    distance_group.add_argument("--no-distance-limit", action="store_true")
    parser.add_argument("--fade-start-m", type=float, default=250.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        track_path = Path(args.track)
        if not track_path.is_file():
            raise FileNotFoundError(f"pure-rotation track not found: {track_path}")
        artifacts = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
        stage_dir = artifacts.stage_dir("render", "08_render")
        camera_path = write_camera_path_csv(
            json.loads(track_path.read_text(encoding="utf-8-sig")),
            stage_dir / "pure_rotation_camera_path.csv",
            origin_xy=tuple(args.origin_xy) if args.origin_xy else (0.0, 0.0),
            cad_scale=float(args.cad_scale or 1.0),
        )
        output_video = stage_dir / "sfm_align_overlay.mp4"
        config = RenderOverlayConfig(
            video_path=args.video,
            cad_dir=args.cad_dir,
            sfm_camera_path=camera_path,
            output_video=output_video,
            cad_scale=args.cad_scale,
            origin_xy=tuple(args.origin_xy) if args.origin_xy else None,
            overlay_linewidth=args.overlay_linewidth,
            overlay_alpha=args.overlay_alpha,
            faded_overlay=args.faded_overlay,
            max_distance_m=(
                None if args.no_distance_limit else args.max_distance_m
            ),
            fade_start_m=args.fade_start_m,
        )
        result = render_overlay_video(config)
        inputs = {
            "video": args.video,
            "cad_dir": args.cad_dir,
            "track": str(track_path),
            "camera_path": str(camera_path),
            "trajectory_mode": "pure_rotation_only",
            "translation_observable": False,
        }
        write_render_outputs(stage_dir, result, inputs=inputs)
        artifacts.record_stage(
            stage_name="render",
            command=[sys.executable, "-m", "cadscene.cli.render_pure_rotation", *(argv or sys.argv[1:])],
            inputs=inputs,
            outputs={
                "output_video": output_video,
                "camera_path": camera_path,
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
