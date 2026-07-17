from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from cadscene.core.io import write_json, write_text
from cadscene.sfm.backend_detection import detect_sfm_environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在相同视频片段上比较 SfM 后端速度与质量。")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id-prefix", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--video", required=True)
    parser.add_argument("--report-dir", default="reports")
    parser.add_argument("--colmap-exe")
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--num-frames", type=int, default=251)
    parser.add_argument("--frame-step", type=int, default=5)
    parser.add_argument("--gpu-index", default="0")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _specs() -> list[dict[str, str]]:
    return [
        {"name": "pycolmap", "backend": "pycolmap", "device": "auto"},
        {"name": "colmap_cli_cpu", "backend": "colmap_cli", "device": "cpu"},
        {"name": "colmap_cli_cuda", "backend": "colmap_cli", "device": "cuda"},
    ]


def _command(args, spec: dict[str, str]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "cadscene.cli.run_sfm",
        "--dataset",
        args.dataset,
        "--run-id",
        f"{args.run_id_prefix}_{spec['name']}",
        "--output-root",
        str(args.output_root),
        "--video",
        str(args.video),
        "--start-frame",
        str(args.start_frame),
        "--num-frames",
        str(args.num_frames),
        "--frame-step",
        str(args.frame_step),
        "--backend",
        spec["backend"],
        "--device",
        spec["device"],
        "--gpu-index",
        str(args.gpu_index),
        "--no-mask",
    ]
    if args.colmap_exe:
        command.extend(["--colmap-exe", str(args.colmap_exe)])
    return command


def assess_reconstruction_quality(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    successful = [item for item in rows if item.get("status") == "success"]
    if not successful:
        return rows
    baseline = next((item for item in successful if item.get("name") == "pycolmap"), successful[0])
    baseline["quality_comparison"] = "baseline"
    baseline["quality_warnings"] = []
    base_registered = float(baseline.get("registered_ratio") or 0.0)
    base_points = int(baseline.get("point_count") or 0)
    base_error = baseline.get("mean_reprojection_error")
    for item in successful:
        if item is baseline:
            continue
        warnings: list[str] = []
        registered = float(item.get("registered_ratio") or 0.0)
        points = int(item.get("point_count") or 0)
        error = item.get("mean_reprojection_error")
        if base_registered > 0 and registered < base_registered * 0.9:
            warnings.append("registered ratio dropped by more than 10%")
        if base_points > 0 and points < base_points * 0.7:
            warnings.append("sparse point count dropped by more than 30%")
        if base_error not in (None, 0) and error is not None and float(error) > float(base_error) * 1.5:
            warnings.append("mean reprojection error increased by more than 50%")
        item["quality_comparison"] = "degraded" if warnings else "comparable"
        item["quality_warnings"] = warnings
    return rows


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SfM 后端基准报告",
        "",
        f"- 视频：{payload['video']}",
        f"- 抽帧：start={payload['start_frame']}，num_frames={payload['num_frames']}，step={payload['frame_step']}",
        "",
        "| 后端 | 状态 | 总耗时(s) | 注册帧 | 稀疏点 | 重投影误差 | 实际 GPU |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["backends"]:
        lines.append(
            "| {name} | {status} | {elapsed} | {registered} | {points} | {error} | {gpu} |".format(
                name=item["name"],
                status=item["status"],
                elapsed=item.get("elapsed_sec", "-"),
                registered=item.get("registered_count", "-"),
                points=item.get("point_count", "-"),
                error=item.get("mean_reprojection_error", "-"),
                gpu="是" if item.get("effective_device") == "cuda" else "否",
            )
        )
    lines.extend(
        [
            "",
            "说明：只有 COLMAP/pycolmap 能力探测与运行日志均确认时，才将实际设备记录为 CUDA。",
            "基准结果同时比较注册帧、点数和重投影误差，不能仅以耗时判断后端优劣。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    video = Path(args.video)
    if not video.exists():
        raise SystemExit(f"video does not exist: {video}")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    environment = detect_sfm_environment(args.colmap_exe)
    rows: list[dict[str, Any]] = []
    for spec in _specs():
        command = _command(args, spec)
        row: dict[str, Any] = {**spec, "command": command}
        if args.dry_run:
            row["status"] = "dry_run"
            rows.append(row)
            continue
        if spec["backend"] == "colmap_cli" and not environment["colmap_cli_available"]:
            row.update(status="skipped", warning="COLMAP CLI unavailable")
            rows.append(row)
            continue
        if spec["backend"] == "pycolmap" and not environment["pycolmap_available"]:
            row.update(status="skipped", warning="pycolmap unavailable")
            rows.append(row)
            continue
        started = time.perf_counter()
        completed = subprocess.run(command, check=False, shell=False)
        row["elapsed_sec"] = round(time.perf_counter() - started, 3)
        row["status"] = "success" if completed.returncode == 0 else "failed"
        stats_path = (
            Path(args.output_root)
            / args.dataset
            / f"{args.run_id_prefix}_{spec['name']}"
            / "02_sfm"
            / "sfm_stats.json"
        )
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
            for key in (
                "registered_count",
                "registered_ratio",
                "point_count",
                "mean_reprojection_error",
                "effective_device",
                "feature_extraction_gpu",
                "feature_matching_gpu",
                "stage_timings",
            ):
                row[key] = stats.get(key)
        rows.append(row)
    rows = assess_reconstruction_quality(rows)
    payload = {
        "schema_version": "cadscene_sfm_backend_benchmark_v1",
        "video": str(video),
        "start_frame": args.start_frame,
        "num_frames": args.num_frames,
        "frame_step": args.frame_step,
        "environment": environment,
        "backends": rows,
    }
    write_json(report_dir / "sfm_backend_benchmark.json", payload)
    write_text(report_dir / "sfm_backend_benchmark.md", _report_markdown(payload))
    print(f"benchmark report written to {report_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
