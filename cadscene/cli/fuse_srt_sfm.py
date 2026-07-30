"""Fuse partial-SRT position constraints with an existing SfM trajectory."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from cadscene.srt.coordinates import build_local_enu
from cadscene.srt.fusion import build_fused_trajectory_json, estimate_sfm_to_srt_sim3, fuse_positions
from cadscene.srt.parser import _parse_records
from cadscene.srt.quality import FusionConfig
from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import load_frame_timestamps, sample_srt_at_frames


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _atomic_write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="将 partial-SRT 与 SfM 轨迹融合到本地 ENU 坐标。")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", default="runs")
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--srt", required=True)
    parser.add_argument("--video")
    parser.add_argument("--frame-timestamps")
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--time-offset-sec", type=float, default=0.0)
    parser.add_argument("--max-interpolation-gap-sec", type=float, default=1.5)
    parser.add_argument("--smoothing-window-sec", type=float, default=2.0)
    parser.add_argument("--min-smoothing-support", type=int, default=3)
    parser.add_argument("--min-common-frames", type=int, default=12)
    parser.add_argument("--min-baseline-m", type=float, default=5.0)
    parser.add_argument("--vertical-weight", type=float, default=0.5)
    parser.add_argument("--height-source", choices=("auto", "rel_alt", "abs_alt"), default="auto")
    parser.add_argument("--export-diagnostics", action="store_true")
    return parser


def _frame_timestamp_path(args: argparse.Namespace) -> Path:
    return Path(args.frame_timestamps) if args.frame_timestamps else Path(args.trajectory).parent / "frame_timestamps.csv"


def _run(args: argparse.Namespace) -> dict[str, Any]:
    raw = json.loads(Path(args.trajectory).read_text(encoding="utf-8-sig"))
    frame_times = {row.source_frame_index: row for row in load_frame_timestamps(str(_frame_timestamp_path(args)))}
    registered = [pose for pose in raw.get("poses", []) if pose.get("registered", True)]
    if not registered:
        raise ValueError("SfM trajectory has no registered poses")
    missing_times = [int(pose["frame_index"]) for pose in registered if int(pose["frame_index"]) not in frame_times]
    if missing_times:
        raise ValueError(f"frame_timestamps.csv lacks registered source frames: {missing_times[:5]}")
    with Path(args.srt).open("rb") as stream:
        records = _parse_records(stream)
    if not records:
        raise ValueError("SRT contains no parseable records; current partial-SRT is unsuitable for fusion, fall back to sfm_only")
    ordered_times = [frame_times[int(pose["frame_index"])] for pose in registered]
    samples = sample_srt_at_frames(
        records,
        frame_timestamps=ordered_times,
        max_interpolation_gap_sec=args.max_interpolation_gap_sec,
        time_scale=args.time_scale,
        time_offset_sec=args.time_offset_sec,
    )
    enu_points, coordinate_meta = build_local_enu(samples, height_source=args.height_source)
    enu_by_sample = {point.source_index: point for point in enu_points if point.up_m is not None}
    valid_indices = [index for index, sample in enumerate(samples) if sample.gps_valid and sample.height_valid and index in enu_by_sample]
    if len(valid_indices) < 3:
        raise ValueError("SRT has too few GPS+height matched frames; current partial-SRT is unsuitable for fusion, fall back to sfm_only")
    source_all = np.asarray([pose["center"] for pose in registered], dtype=np.float64)
    source = source_all[valid_indices]
    target = np.asarray([[enu_by_sample[index].east_m, enu_by_sample[index].north_m, enu_by_sample[index].up_m] for index in valid_indices], dtype=np.float64)
    config = FusionConfig(
        min_common_frames=args.min_common_frames,
        min_baseline_m=args.min_baseline_m,
        vertical_weight=args.vertical_weight,
    )
    fit = estimate_sfm_to_srt_sim3(source, target, config)
    metric_all = fit.transform(source_all)
    srt_all = metric_all.copy()
    valid = np.zeros(len(registered), dtype=bool)
    for index in valid_indices:
        point = enu_by_sample[index]
        srt_all[index] = [point.east_m, point.north_m, point.up_m]
        valid[index] = True
    position_result = fuse_positions(
        metric_all,
        frame_times_sec=np.asarray([row.pts_time_sec for row in ordered_times]),
        srt_positions=srt_all,
        srt_valid=valid,
        smoothing_window_sec=args.smoothing_window_sec,
        min_smoothing_support=args.min_smoothing_support,
    )
    fused = build_fused_trajectory_json(raw, position_result, sim3_rotation=fit.rotation, coordinate_meta={**coordinate_meta, "time_scale": args.time_scale, "time_offset_sec": args.time_offset_sec})
    return {"records": records, "samples": samples, "fit": fit, "fused": fused, "position_result": position_result, "valid_count": int(valid.sum())}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = Path(args.output_root) / args.dataset / args.run_id
    srt_dir, fusion_dir = run_dir / "02_srt", run_dir / "02_fusion"
    try:
        result = _run(args)
        fit = result["fit"]
        samples = result["samples"]
        _atomic_write_json(srt_dir / "srt_trajectory.json", {"entry_count": len(result["records"]), "matched_frame_count": result["valid_count"]})
        _atomic_write_csv(
            srt_dir / "srt_frame_samples.csv",
            [
                {"frame_index": sample.frame_timestamp.source_frame_index, "frame_time_sec": sample.frame_timestamp.pts_time_sec, "srt_time_sec": sample.srt_time_sec, "latitude": sample.latitude, "longitude": sample.longitude, "rel_alt": sample.rel_alt, "abs_alt": sample.abs_alt, "gps_valid": sample.gps_valid, "height_valid": sample.height_valid, "interpolated": sample.interpolated, "source_entry_before": sample.source_entry_before, "source_entry_after": sample.source_entry_after}
                for sample in samples
            ],
            ["frame_index", "frame_time_sec", "srt_time_sec", "latitude", "longitude", "rel_alt", "abs_alt", "gps_valid", "height_valid", "interpolated", "source_entry_before", "source_entry_after"],
        )
        _atomic_write_json(fusion_dir / "camera_trajectory_fused.json", result["fused"])
        _atomic_write_json(fusion_dir / "fusion_stats.json", {"common_frame_count": result["valid_count"], "scale": fit.scale, "inlier_ratio": fit.inlier_ratio, "rmse_m": fit.rmse_m, "xy_rmse_m": fit.xy_rmse_m, "z_rmse_m": fit.z_rmse_m})
        _atomic_write_text(fusion_dir / "fusion_report.md", f"# Partial-SRT 融合报告\n\n- 公共帧数：{result['valid_count']}\n- Sim3 scale：{fit.scale:.6f}\n- Sim3 RMSE：{fit.rmse_m:.3f} m\n- XY RMSE：{fit.xy_rmse_m:.3f} m\n- Z RMSE：{fit.z_rmse_m:.3f} m\n- 内点比例：{fit.inlier_ratio:.3f}\n- 方向来源：SfM\n- 置信度仅表示内部一致性，不代表绝对定位精度。\n")
        print(f"公共帧数: {result['valid_count']}")
        print(f"Sim3 scale: {fit.scale:.6f}")
        print(f"Sim3 RMSE: {fit.rmse_m:.3f} m")
        print(f"输出路径: {fusion_dir}")
        return 0
    except Exception as exc:
        _atomic_write_text(fusion_dir / "fusion_failure_report.md", f"# Partial-SRT 融合失败\n\n{exc}\n\n建议：当前 partial-SRT 不适合融合，可回退到 sfm_only。\n")
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
