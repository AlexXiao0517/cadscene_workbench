"""Offline Rank-1 partial-SRT + manual-prior alignment into CAD metres."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from cadscene.alignment.keyframe_schema import normalize_camera_track
from cadscene.alignment.orientation_prior import build_orientation_prior, validate_prior_split
from cadscene.alignment.rank1_constrained import (
    Rank1Config,
    analyze_rank1_axis,
    apply_along_track_correction,
    apply_vertical_srt_constraint,
    build_rank1_trajectory_json,
    estimate_along_track_scale,
    project_along_track,
    solve_rank1_transform,
)
from cadscene.alignment.rank1_validation import validate_holdout_anchors
from cadscene.core.camera import decompose_world_from_camera_rotation
from cadscene.sfm.trajectory import load_sfm_trajectory, quat_wxyz_to_matrix
from cadscene.srt.coordinates import build_local_enu


@dataclass(frozen=True)
class SrtSample:
    frame_index: int
    frame_time_sec: float
    position_enu: np.ndarray
    valid: bool
    relative_height_m: float | None
    height_source: str


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


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(fieldnames))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _boolean(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _number(row: Mapping[str, Any], name: str) -> float | None:
    try:
        value = float(row.get(name, ""))
    except (TypeError, ValueError):
        return None
    return value if np.isfinite(value) else None


def _load_srt_samples(path: Path) -> list[SrtSample]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any("frame_index" not in row or "frame_time_sec" not in row for row in rows):
        raise ValueError("srt-samples CSV requires frame_index and frame_time_sec")
    rel_altitudes = [_number(row, "rel_alt") for row in rows]
    abs_altitudes = [_number(row, "abs_alt") for row in rows]
    if any(value is not None for value in rel_altitudes):
        selected_heights = rel_altitudes
        height_source = "rel_alt_relative"
    elif any(value is not None for value in abs_altitudes):
        selected_heights = abs_altitudes
        height_source = "abs_alt_relative"
    else:
        raise ValueError("srt-samples CSV requires rel_alt or abs_alt for vertical constraint")
    first_height = next(value for value in selected_heights if value is not None)

    direct_enu = all(all(name in row for name in ("east_m", "north_m", "up_m")) for row in rows)
    positions: dict[int, np.ndarray] = {}
    if direct_enu:
        for index, row in enumerate(rows):
            values = [_number(row, name) for name in ("east_m", "north_m", "up_m")]
            if all(value is not None for value in values):
                positions[index] = np.asarray(values, dtype=np.float64)
    else:
        enu, _meta = build_local_enu(rows, height_source="auto")
        positions = {
            point.source_index: np.asarray([point.east_m, point.north_m, point.up_m], dtype=np.float64)
            for point in enu
            if point.up_m is not None
        }
    samples: list[SrtSample] = []
    for index, row in enumerate(rows):
        frame = int(row["frame_index"])
        time = _number(row, "frame_time_sec")
        if time is None:
            raise ValueError(f"SRT sample frame {frame} has invalid frame_time_sec")
        declared_valid = _boolean(row.get("gps_valid", True)) and _boolean(row.get("height_valid", True))
        selected_height = selected_heights[index]
        relative_height = None if selected_height is None else float(selected_height - first_height)
        valid = declared_valid and index in positions and relative_height is not None
        position = positions.get(index, np.asarray([np.nan, np.nan, np.nan], dtype=np.float64))
        samples.append(SrtSample(frame, time, position, valid, relative_height, height_source))
    return samples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 partial-SRT 沿程尺度和人工姿态先验执行 Rank-1 SfM→CAD 离线对齐。")
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--srt-samples", required=True)
    parser.add_argument("--camera-track", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--schema-mode", choices=("strict", "legacy"), default="strict")
    parser.add_argument("--cad-scale", type=float, default=1.0)
    parser.add_argument("--origin-x", type=float, default=0.0)
    parser.add_argument("--origin-y", type=float, default=0.0)
    parser.add_argument("--smoothing-window-sec", type=float, default=2.0)
    parser.add_argument("--min-smoothing-support", type=int, default=3)
    parser.add_argument("--vertical-smoothing-window-sec", type=float, default=2.0)
    parser.add_argument("--min-vertical-smoothing-support", type=int, default=3)
    parser.add_argument("--max-sfm-vertical-detail-m", type=float, default=0.5)
    parser.add_argument("--min-direction-angle-deg", type=float, default=20.0)
    parser.add_argument("--min-baseline-m", type=float, default=5.0)
    parser.add_argument("--along-track-inlier-threshold-m", type=float, default=2.0)
    parser.add_argument("--ransac-seed", type=int, default=20_260_721)
    parser.add_argument("--require-validate-anchor", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--export-diagnostics", action="store_true")
    return parser


def _config(args: argparse.Namespace) -> Rank1Config:
    return Rank1Config(
        min_baseline=float(args.min_baseline_m),
        along_track_inlier_threshold_m=float(args.along_track_inlier_threshold_m),
        ransac_seed=int(args.ransac_seed),
        smoothing_window_sec=float(args.smoothing_window_sec),
        min_smoothing_support=int(args.min_smoothing_support),
        vertical_smoothing_window_sec=float(args.vertical_smoothing_window_sec),
        min_vertical_smoothing_support=int(args.min_vertical_smoothing_support),
        max_sfm_vertical_detail_m=float(args.max_sfm_vertical_detail_m),
        min_direction_angle_deg=float(args.min_direction_angle_deg),
    )


def _alignment_payload(result: dict[str, Any]) -> dict[str, Any]:
    transform = result["transform"]
    scale_fit = result["scale_fit"]
    validation = result["validation"]
    return {
        "method": "rank1_srt_manual_orientation",
        "coordinate_system": "cad_meters",
        "trajectory_rank": 1,
        "scale_source": "srt_along_track",
        "orientation_source": "manual_orientation_prior",
        "translation_source": "manual_solve_anchor_position",
        "srt_constraint": "along_track_and_relative_height",
        "vertical_source": "srt_relative_altitude_primary",
        "height_datum": "manual_solve_anchors",
        "absolute_elevation_available": False,
        "solve_frame_indices": list(transform.solve_frame_indices),
        "validate_frame_indices": [metric.source_frame_index for metric in validation.metrics],
        "scale": float(transform.scale),
        "rotation_cad_from_sfm": transform.rotation_cad_from_sfm.tolist(),
        "translation_cad_from_sfm": transform.translation_cad_from_sfm.tolist(),
        "quality": {
            "scale_inlier_count": scale_fit.inlier_count,
            "scale_inlier_ratio": scale_fit.inlier_ratio,
            "median_along_track_residual_m": scale_fit.median_along_track_residual_m,
            "rmse_along_track_residual_m": scale_fit.rmse_along_track_residual_m,
            "p90_along_track_residual_m": scale_fit.p90_along_track_residual_m,
            "scale_consistency": scale_fit.scale_consistency,
            "rotation_disagreement_deg": transform.rotation_disagreement_deg,
            "along_translation_spread_m": transform.along_translation_spread_m,
            "lateral_translation_spread_m": transform.lateral_translation_spread_m,
            "solve_height_offset_range_m": transform.solve_height_offset_range_m,
            "solve_height_offset_mad_m": transform.solve_height_offset_mad_m,
            "holdout_accepted": validation.accepted,
        },
        "warnings": list(result["warnings"]),
    }


def _run(args: argparse.Namespace) -> dict[str, Any]:
    raw = json.loads(Path(args.trajectory).read_text(encoding="utf-8-sig"))
    trajectory = load_sfm_trajectory(args.trajectory)
    samples = _load_srt_samples(Path(args.srt_samples))
    sample_by_frame = {sample.frame_index: sample for sample in samples if sample.valid}
    common_indices = [index for index, frame in enumerate(trajectory.frames) if int(frame) in sample_by_frame]
    if len(common_indices) < 6:
        raise ValueError("Rank-1 synchronization has too few common valid frames")
    common_sfm = trajectory.centers[common_indices]
    common_srt = np.asarray([sample_by_frame[int(trajectory.frames[index])].position_enu for index in common_indices], dtype=np.float64)
    common_times = np.asarray([sample_by_frame[int(trajectory.frames[index])].frame_time_sec for index in common_indices], dtype=np.float64)
    config = _config(args)
    sfm_axis = analyze_rank1_axis(common_sfm, common_times, Rank1Config(**{**asdict(config), "min_baseline": max(1e-6, min(config.min_baseline, 0.1))}))
    srt_axis = analyze_rank1_axis(common_srt, common_times, config)
    u_sfm = project_along_track(common_sfm, sfm_axis.primary_direction, common_sfm[0])
    u_srt = project_along_track(common_srt, srt_axis.primary_direction, common_srt[0])
    scale_fit = estimate_along_track_scale(u_sfm, u_srt, config)

    raw_track = json.loads(Path(args.camera_track).read_text(encoding="utf-8-sig"))
    normalized = normalize_camera_track(raw_track)
    if args.schema_mode == "strict" and normalized["schema_version"] != 2:
        raise ValueError("strict schema mode requires camera track schema_version=2")
    priors = [
        build_orientation_prior(
            keyframe,
            origin_xy=(float(args.origin_x), float(args.origin_y)),
            cad_scale=float(args.cad_scale),
        )
        for keyframe in normalized["keyframes"]
    ]
    split = validate_prior_split(priors)
    if args.require_validate_anchor and not split.accepted:
        raise ValueError(f"holdout protocol is incomplete ({', '.join(split.rejection_reasons)})")
    relative_height_by_frame = {
        sample.frame_index: float(sample.relative_height_m)
        for sample in samples
        if sample.valid and sample.relative_height_m is not None
    }
    transform = solve_rank1_transform(
        trajectory,
        scale_fit,
        sfm_axis.primary_direction,
        priors,
        config,
        srt_relative_height_by_frame=relative_height_by_frame,
    )
    base = transform.apply_points(trajectory.centers)
    d_cad = transform.rotation_cad_from_sfm @ sfm_axis.primary_direction

    base_u = project_along_track(base, d_cad, base[0])
    target = np.zeros(len(trajectory.frames), dtype=np.float64)
    valid = np.zeros(len(trajectory.frames), dtype=bool)
    common_target = project_along_track(common_srt, srt_axis.primary_direction, common_srt[0])
    offset = float(np.median(common_target - base_u[common_indices]))
    for local_index, trajectory_index in enumerate(common_indices):
        target[trajectory_index] = common_target[local_index] - offset
        valid[trajectory_index] = True
    frame_times = np.asarray([
        sample_by_frame[int(frame)].frame_time_sec if int(frame) in sample_by_frame else float(frame) / trajectory.fps
        for frame in trajectory.frames
    ], dtype=np.float64)
    correction = apply_along_track_correction(base, d_cad, frame_times, target, valid, config)
    relative_height = np.full(len(trajectory.frames), np.nan, dtype=np.float64)
    height_valid = np.zeros(len(trajectory.frames), dtype=bool)
    for index, frame in enumerate(trajectory.frames):
        sample = sample_by_frame.get(int(frame))
        if sample is not None and sample.relative_height_m is not None:
            relative_height[index] = float(sample.relative_height_m)
            height_valid[index] = True
    vertical = apply_vertical_srt_constraint(
        correction.fused_positions,
        frame_times,
        relative_height,
        height_valid,
        height_offset_m=transform.height_offset_m,
        config=config,
    )
    validation = validate_holdout_anchors(trajectory, vertical.fused_positions, transform, d_cad, priors, config)
    if args.require_validate_anchor and not validation.accepted:
        raise ValueError(f"holdout validation failed ({', '.join(validation.rejection_reasons)})")
    fused = build_rank1_trajectory_json(
        raw,
        transform,
        vertical.fused_positions,
        correction,
        vertical,
    )
    if not np.isfinite(vertical.fused_positions).all():
        raise ValueError("Rank-1 output contains NaN or Inf")
    return {
        "raw": raw,
        "trajectory": trajectory,
        "samples": samples,
        "prior_split": split,
        "common_indices": common_indices,
        "sfm_axis": sfm_axis,
        "srt_axis": srt_axis,
        "scale_fit": scale_fit,
        "transform": transform,
        "base": base,
        "d_cad": d_cad,
        "correction": correction,
        "vertical_correction": vertical,
        "validation": validation,
        "fused": fused,
        "warnings": (),
    }


def _camera_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    trajectory = result["trajectory"]
    output_poses = [pose for pose in result["fused"]["poses"] if pose.get("registered", True)]
    fov = float(trajectory.horizontal_fov_deg() or 70.0)
    rows: list[dict[str, Any]] = []
    for pose in output_poses:
        world_from_camera = quat_wxyz_to_matrix(pose["cam_from_world_quat_wxyz"]).T
        yaw, pitch, roll = decompose_world_from_camera_rotation(world_from_camera)
        rows.append({
            "frame_index": int(pose["frame_index"]), "x": pose["center"][0], "y": pose["center"][1], "z": pose["center"][2],
            "yaw": yaw, "pitch": pitch, "roll": roll, "fov": fov,
            "trajectory_source": "rank1_sfm_srt_along_track_cad", "srt_valid": pose.get("srt_valid", False),
            "along_track_correction_m": pose.get("along_track_correction_m", 0.0),
            "srt_height_valid": pose.get("srt_height_valid", False),
            "srt_relative_height_m": pose.get("srt_relative_height_m"),
            "vertical_correction_m": pose.get("vertical_correction_m", 0.0),
            "vertical_smoothing_support": pose.get("vertical_smoothing_support", 0),
            "sfm_vertical_detail_m": pose.get("sfm_vertical_detail_m", 0.0),
        })
    return rows


def _validation_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [asdict(metric) for metric in result["validation"].metrics]


def _comparison_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    trajectory = result["trajectory"]
    correction = result["correction"]
    vertical = result["vertical_correction"]
    return [
        {
            "frame_index": int(frame),
            "base_x": correction.base_positions[index, 0], "base_y": correction.base_positions[index, 1], "base_z": correction.base_positions[index, 2],
            "fused_x": vertical.fused_positions[index, 0], "fused_y": vertical.fused_positions[index, 1], "fused_z": vertical.fused_positions[index, 2],
            "srt_valid": bool(correction.srt_valid[index]),
            "raw_delta_u_m": None if not correction.srt_valid[index] else correction.raw_delta_u_m[index],
            "smoothed_delta_u_m": correction.smoothed_delta_u_m[index],
            "smoothing_support": int(correction.smoothing_support[index]),
            "srt_height_valid": bool(vertical.srt_height_valid[index]),
            "srt_relative_height_m": None if not vertical.srt_height_valid[index] else vertical.srt_relative_height_m[index],
            "vertical_correction_m": vertical.vertical_correction_m[index],
            "vertical_smoothing_support": int(vertical.vertical_smoothing_support[index]),
            "sfm_vertical_detail_m": vertical.sfm_vertical_detail_m[index],
        }
        for index, frame in enumerate(trajectory.frames)
    ]


def _report(result: dict[str, Any]) -> str:
    scale = result["scale_fit"]
    validation = result["validation"]
    return "\n".join([
        "# Rank-1 Partial-SRT 人工姿态约束对齐报告", "",
        "- 方法：Rank-1 SfM→CAD constrained alignment",
        "- 输出坐标系：CAD meters",
        "- SRT 约束：仅沿程尺度与低频沿程残差",
        "- 姿态来源：SfM + 人工 solve orientation prior",
        f"- 公共帧数：{len(result['common_indices'])}",
        f"- 沿程 scale：{scale.scale:.9g}",
        f"- scale 内点比例：{scale.inlier_ratio:.6f}",
        f"- 沿程残差 RMSE：{scale.rmse_along_track_residual_m:.6f} m",
        f"- solve frames：{list(result['transform'].solve_frame_indices)}",
        f"- validate frames：{[metric.source_frame_index for metric in validation.metrics]}",
        f"- hold-out 验证：{'通过' if validation.accepted else '未通过'}", "",
        "solve anchor 用于姿态与平移求解；validate anchor 不参与任何求解或平滑参数估计。",
        "本接口尚未接入正式前端、Job Runner 或 Stage 6B-2。", "",
    ])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output_dir)
    try:
        result = _run(args)
        if not result["prior_split"].accepted or not result["validation"].accepted:
            reasons = list(result["prior_split"].rejection_reasons) + list(result["validation"].rejection_reasons)
            raise ValueError(f"diagnostic-only result is not eligible for formal output ({', '.join(dict.fromkeys(reasons))})")
        alignment = _alignment_payload(result)
        stats = {
            "trajectory_rank": 1,
            "sfm_singular_values": list(result["sfm_axis"].singular_values),
            "srt_singular_values": list(result["srt_axis"].singular_values),
            "sfm_linearity_ratio": result["sfm_axis"].linearity_ratio,
            "srt_linearity_ratio": result["srt_axis"].linearity_ratio,
            "vertical_height_source": next(
                sample.height_source for sample in result["samples"] if sample.valid
            ),
            "vertical_height_coverage_ratio": float(
                np.mean([sample.relative_height_m is not None for sample in result["samples"]])
            ),
            "vertical_smoothing_window_sec": float(args.vertical_smoothing_window_sec),
            "min_vertical_smoothing_support": int(args.min_vertical_smoothing_support),
            "max_sfm_vertical_detail_m": float(args.max_sfm_vertical_detail_m),
            **alignment["quality"],
        }
        _atomic_write_json(output / "rank1_alignment.json", alignment)
        _atomic_write_json(output / "camera_trajectory_rank1_cad.json", result["fused"])
        camera_rows = _camera_rows(result)
        _atomic_write_csv(output / "camera_path_rank1_cad.csv", camera_rows, list(camera_rows[0]))
        _atomic_write_json(output / "rank1_alignment_stats.json", stats)
        _atomic_write_text(output / "rank1_alignment_report.md", _report(result))
        validation_rows = _validation_rows(result)
        _atomic_write_csv(output / "rank1_validation.csv", validation_rows, list(validation_rows[0]))
        comparison_rows = _comparison_rows(result)
        _atomic_write_csv(output / "rank1_trajectory_comparison.csv", comparison_rows, list(comparison_rows[0]))
        print(f"Rank-1 scale: {result['scale_fit'].scale:.9g}")
        print(f"Hold-out anchors: {len(result['validation'].metrics)}")
        print(f"输出目录: {output}")
        return 0
    except Exception as exc:
        _atomic_write_text(
            output / "rank1_failure_report.md",
            f"# Rank-1 对齐失败\n\n- error_code: `RANK1_ALIGNMENT_REJECTED`\n- reason: {exc}\n\n未生成正式 Rank-1 trajectory；请检查 rank、同步和人工 solve/validate 姿态先验。\n",
        )
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
