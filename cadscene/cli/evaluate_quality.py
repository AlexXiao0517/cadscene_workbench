from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cadscene.alignment.quality import (
    QualityConfig,
    augment_camera_track_with_quality,
    build_keyframe_suggestions_payload,
    evaluate_quality,
    load_sfm_camera_path,
    quality_report,
)
from cadscene.core.artifacts import ArtifactManager
from cadscene.core.io import read_json, write_csv_utf8_sig, write_json, write_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SfM-CAD 对齐质量评估与关键帧建议（只读，不改 pose）。")
    parser.add_argument("--config", default=None, help="pipeline 配置路径；Stage 3B 暂只记录。")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--sfm-camera-path", required=True)
    parser.add_argument("--alignment", required=True)
    parser.add_argument("--web-camera-track", required=True)
    parser.add_argument("--camera-track-pred", default=None)
    parser.add_argument("--trajectory", default=None)
    parser.add_argument("--cad-dir", required=True)
    parser.add_argument("--cad-scale", type=float, required=True)
    parser.add_argument("--origin-xy", type=float, nargs=2, required=True, metavar=("X", "Y"))
    parser.add_argument("--quality-mode", choices=["bootstrap", "qa"], default="bootstrap")
    parser.add_argument("--allow-bootstrap-correction", action="store_true")
    parser.add_argument("--adaptive-percentile", type=float, default=85.0)
    parser.add_argument("--max-suggestions", type=int, default=10)
    parser.add_argument("--min-suggestion-gap", type=int, default=40)
    parser.add_argument("--suggestion-risk-threshold", type=float, default=0.65)
    parser.add_argument("--seg-dir", default=None)
    parser.add_argument("--no-suggestion-samples", action="store_true")
    return parser


def _write_optional_plot(path: Path, timeline: list[dict]) -> str | None:
    try:
        import matplotlib.pyplot as plt

        frames = [int(row["frame_index"]) for row in timeline]
        risks = [float(row["risk_score"]) for row in timeline]
        fig, ax = plt.subplots(figsize=(8, 3))
        ax.plot(frames, risks, color="#1f77b4", linewidth=1.5)
        ax.set_xlabel("frame_index")
        ax.set_ylabel("risk_score")
        ax.set_ylim(0, 1)
        fig.tight_layout()
        path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(path, dpi=120)
        plt.close(fig)
        return None
    except Exception as exc:
        return f"quality_diagnostics.png skipped: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = ArtifactManager(output_root=args.output_root, dataset=args.dataset, run_id=args.run_id)
    stage_dir = manager.stage_dir("quality", "04_quality")
    sfm_rows = load_sfm_camera_path(args.sfm_camera_path)
    alignment = read_json(args.alignment)
    web_track = read_json(args.web_camera_track)
    pred_source = read_json(args.camera_track_pred) if args.camera_track_pred else web_track
    trajectory = read_json(args.trajectory) if args.trajectory else None
    config = QualityConfig(
        quality_mode=args.quality_mode,
        allow_bootstrap_correction=args.allow_bootstrap_correction,
        adaptive_percentile=args.adaptive_percentile,
        max_suggestions=args.max_suggestions,
        min_suggestion_gap=args.min_suggestion_gap,
        suggestion_risk_threshold=args.suggestion_risk_threshold,
        seg_dir=args.seg_dir,
    )
    result = evaluate_quality(
        sfm_camera_path_rows=sfm_rows,
        alignment_json=alignment,
        web_camera_track=web_track,
        trajectory_json=trajectory,
        config=config,
    )
    diag_warning = _write_optional_plot(stage_dir / "quality_diagnostics.png", result.timeline)
    if diag_warning:
        result.warnings.append(diag_warning)
    timeline_path = stage_dir / "quality_timeline.csv"
    suggestions_path = stage_dir / "keyframe_suggestions.json"
    report_path = stage_dir / "quality_report.md"
    pred_quality_path = stage_dir / "camera_track_pred_quality.json"
    write_csv_utf8_sig(timeline_path, result.timeline)
    write_json(suggestions_path, build_keyframe_suggestions_payload(result.suggestions, result.meta))
    write_json(pred_quality_path, augment_camera_track_with_quality(pred_source, result.timeline, result.suggestion_frames, result.meta))
    algorithm_prediction_count = sum(1 for item in pred_source.get("keyframes", []) if item.get("source") == "algorithm_prediction")
    write_text(
        report_path,
        quality_report(
            result=result,
            inputs={
                "sfm_camera_path": args.sfm_camera_path,
                "alignment": args.alignment,
                "web_camera_track": args.web_camera_track,
                "camera_track_pred": args.camera_track_pred or "",
                "trajectory": args.trajectory or "",
                "cad_dir": args.cad_dir,
                "seg_dir": args.seg_dir or "",
            },
            algorithm_prediction_count=algorithm_prediction_count,
            adaptive_percentile=args.adaptive_percentile,
        ),
    )
    outputs = {
        "quality_timeline": str(timeline_path),
        "keyframe_suggestions": str(suggestions_path),
        "quality_report": str(report_path),
        "camera_track_pred_quality": str(pred_quality_path),
    }
    diag_path = stage_dir / "quality_diagnostics.png"
    if diag_path.exists():
        outputs["quality_diagnostics"] = str(diag_path)
    manager.record_stage(
        stage_name="quality",
        command=[sys.executable, "-m", "cadscene.cli.evaluate_quality", *sys.argv[1:]],
        inputs={
            "config": args.config,
            "sfm_camera_path": args.sfm_camera_path,
            "alignment": args.alignment,
            "web_camera_track": args.web_camera_track,
            "camera_track_pred": args.camera_track_pred,
            "trajectory": args.trajectory,
            "cad_dir": args.cad_dir,
            "cad_scale": args.cad_scale,
            "origin_xy": args.origin_xy,
        },
        outputs=outputs,
        metrics={
            "timeline_frames": len(result.timeline),
            "suggestion_count": len(result.suggestions),
            "high_count": sum(1 for row in result.timeline if row["risk_level"] == "high"),
        },
        status="success",
    )
    print(f"quality outputs written to {stage_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
