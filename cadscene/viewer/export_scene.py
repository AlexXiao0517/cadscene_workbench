from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from cadscene.alignment.aligner import AlignmentConfig, aligned_state_at_frame
from cadscene.core.camera import CameraState
from cadscene.core.coordinates import cad_meters_to_web_camera, python_state_to_web_camera
from cadscene.core.io import read_json
from cadscene.core.sim3 import Sim3
from cadscene.sfm.pointcloud import load_ply
from cadscene.sfm.trajectory import load_sfm_trajectory
from cadscene.viewer.schema import SCHEMA_VERSION, validate_viewer_scene


@dataclass(frozen=True)
class ExportViewerSceneConfig:
    cad_scale: float = 1.0
    origin_xy: tuple[float, float] = (0.0, 0.0)
    max_points: int = 80000
    point_sample_mode: str = "voxel"
    voxel_size: float = 0.5
    random_seed: int = 0
    trajectory_frame_step: int | None = None


def load_sim3_from_alignment(path: str | Path) -> Sim3:
    data = read_json(path)
    raw = data.get("sim3") or data.get("transform") or data
    return Sim3.from_dict(raw)


def _bbox(points: np.ndarray) -> dict:
    arr = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(arr) == 0:
        return {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    return {"min": arr.min(axis=0).round(6).tolist(), "max": arr.max(axis=0).round(6).tolist()}


def _cad_points_to_web(points_cad_m: np.ndarray, config: ExportViewerSceneConfig) -> np.ndarray:
    return np.asarray(
        [
            [
                cad_meters_to_web_camera(point, config.origin_xy, config.cad_scale)["x"],
                cad_meters_to_web_camera(point, config.origin_xy, config.cad_scale)["y"],
                cad_meters_to_web_camera(point, config.origin_xy, config.cad_scale)["z"],
            ]
            for point in np.asarray(points_cad_m, dtype=np.float64).reshape(-1, 3)
        ],
        dtype=np.float64,
    )


def voxel_downsample_indices(points: np.ndarray, voxel_size: float) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(pts) == 0 or float(voxel_size) <= 0:
        return np.arange(len(pts), dtype=np.int64)
    keys = np.floor(pts / float(voxel_size)).astype(np.int64)
    seen: set[tuple[int, int, int]] = set()
    keep: list[int] = []
    for i, key in enumerate(keys):
        item = tuple(int(v) for v in key)
        if item in seen:
            continue
        seen.add(item)
        keep.append(i)
    return np.asarray(keep, dtype=np.int64)


def sample_point_indices(points: np.ndarray, max_points: int, mode: str, voxel_size: float, seed: int = 0) -> np.ndarray:
    count = len(points)
    if count == 0:
        return np.arange(0, dtype=np.int64)
    mode = str(mode)
    if mode == "voxel":
        idx = voxel_downsample_indices(points, voxel_size)
        if max_points > 0 and len(idx) > max_points:
            rng = np.random.default_rng(seed)
            sub = np.sort(rng.choice(len(idx), size=int(max_points), replace=False))
            idx = idx[sub]
        return idx
    if max_points <= 0 or count <= max_points:
        return np.arange(count, dtype=np.int64)
    if mode == "random":
        rng = np.random.default_rng(seed)
        return np.sort(rng.choice(count, size=int(max_points), replace=False))
    step = count / float(max_points)
    return np.asarray([int(i * step) for i in range(int(max_points))], dtype=np.int64)


def prepare_point_cloud(sparse_ply: str | Path, alignment: str | Path, config: ExportViewerSceneConfig) -> dict:
    sim3 = load_sim3_from_alignment(alignment)
    points_sfm, colors = load_ply(sparse_ply)
    count_original = int(len(points_sfm))
    points_cad_m = sim3.apply(points_sfm)
    idx = sample_point_indices(points_cad_m, config.max_points, config.point_sample_mode, config.voxel_size, seed=config.random_seed)
    sampled_cad_m = points_cad_m[idx]
    sampled_web = _cad_points_to_web(sampled_cad_m, config)
    sampled_colors = colors[idx] if colors is not None else None
    data: list[list[float]] = []
    for i, point in enumerate(sampled_web):
        row = [round(float(point[0]), 6), round(float(point[1]), 6), round(float(point[2]), 6)]
        if sampled_colors is not None:
            row.extend(int(v) for v in sampled_colors[i].tolist())
        data.append(row)
    return {
        "count_original": count_original,
        "count_exported": int(len(sampled_web)),
        "sample_mode": config.point_sample_mode,
        "voxel_size": float(config.voxel_size),
        "has_rgb": sampled_colors is not None,
        "rgb_range": "0_255",
        "bbox": _bbox(sampled_web),
        "data": data,
    }


def _empty_points(config: ExportViewerSceneConfig) -> dict:
    return {
        "count_original": 0,
        "count_exported": 0,
        "sample_mode": "none",
        "voxel_size": float(config.voxel_size),
        "has_rgb": False,
        "rgb_range": "0_255",
        "bbox": _bbox(np.zeros((0, 3), dtype=np.float64)),
        "data": [],
    }


def _read_csv_rows(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_global_sfm_track(trajectory: str | Path, alignment: str | Path, config: ExportViewerSceneConfig) -> list[dict]:
    traj = load_sfm_trajectory(trajectory)
    sim3 = load_sim3_from_alignment(alignment)
    align_cfg = AlignmentConfig(cad_scale=config.cad_scale, origin_xy=config.origin_xy, fov_from="trajectory")
    frames = [int(v) for v in traj.frames.tolist()]
    if config.trajectory_frame_step and config.trajectory_frame_step > 1:
        frames = frames[:: int(config.trajectory_frame_step)]
    out: list[dict] = []
    for frame in frames:
        state = aligned_state_at_frame(frame, traj, sim3, anchored=None, config=align_cfg)
        out.append(
            {
                "frame_index": int(frame),
                "camera": python_state_to_web_camera(state, config.origin_xy),
                "source": "global_sim3_sfm",
            }
        )
    return out


def load_anchored_camera_path(path: str | Path, config: ExportViewerSceneConfig) -> list[dict]:
    out: list[dict] = []
    for row in _read_csv_rows(path):
        frame = int(float(row["frame_index"]))
        state = CameraState(
            camera_x=float(row["camera_x"]),
            camera_y=float(row["camera_y"]),
            camera_z=float(row["camera_z"]),
            yaw_deg=float(row.get("yaw", 0.0)),
            pitch_deg=float(row.get("pitch", 0.0)),
            roll_deg=float(row.get("roll", 0.0)),
            fov_deg=float(row.get("fov", 70.0)),
            cad_scale=float(config.cad_scale),
        )
        out.append(
            {
                "frame_index": frame,
                "camera": python_state_to_web_camera(state, config.origin_xy),
                "source": "segment_anchor_path",
            }
        )
    return out


def load_suggestions(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    data = read_json(path)
    items = data if isinstance(data, list) else data.get("suggestions", [])
    out: list[dict] = []
    for item in items:
        out.append(
            {
                "frame_index": int(item.get("frame_index", item.get("frame", 0))),
                "risk_score": float(item.get("risk_score", 0.0)),
                "risk_level": str(item.get("risk_level", item.get("priority", ""))),
                "priority": str(item.get("priority", item.get("risk_level", ""))),
                "reason": str(item.get("reason", "")),
                "reason_codes": list(item.get("reason_codes", [])),
                "suggest_action": str(item.get("suggest_action", "")),
                "nearest_anchor_frame": item.get("nearest_anchor_frame", ""),
            }
        )
    out.sort(key=lambda row: row["frame_index"])
    return out


def assemble_scene(
    *,
    dataset: str,
    run_id: str,
    points: dict,
    global_track: Sequence[dict],
    anchored_track: Sequence[dict],
    suggestions: Sequence[dict],
    quality_timeline: str | Path | None,
    suggestions_path: str | Path | None,
) -> dict:
    scene = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "generated_by": "cadscene.export_viewer_scene",
            "coordinate_system": "web_cad_world",
            "point_transform": "global_sim3_only",
            "global_track_transform": "global_sim3_only",
            "anchored_track_transform": "segment_anchor_path_from_sfm_camera_path",
            "pitch_convention": "frontend_pitch_negated_from_python_pitch",
            "dataset": dataset,
            "run_id": run_id,
            "rgb_range": "0_255",
        },
        "points": points,
        "tracks": {
            "global_sfm_track": list(global_track),
            "anchored_camera_path": list(anchored_track),
        },
        "suggestions": list(suggestions),
        "quality": {
            "available": bool(quality_timeline and Path(quality_timeline).exists()),
            "quality_timeline_ref": str(quality_timeline) if quality_timeline else "",
            "suggestions_ref": str(suggestions_path) if suggestions_path else "",
        },
    }
    validate_viewer_scene(scene)
    return scene


def scene_stats(scene: Mapping[str, object], warnings: Sequence[str] | None = None) -> dict:
    points = scene.get("points", {})
    tracks = scene.get("tracks", {})
    return {
        "point_count_original": int(points.get("count_original", 0)),
        "point_count_exported": int(points.get("count_exported", 0)),
        "has_rgb": bool(points.get("has_rgb", False)),
        "point_sample_mode": points.get("sample_mode", "none"),
        "voxel_size": points.get("voxel_size", 0.0),
        "global_track_count": len(tracks.get("global_sfm_track", [])),
        "anchored_track_count": len(tracks.get("anchored_camera_path", [])),
        "suggestion_count": len(scene.get("suggestions", [])),
        "bbox_web_cad_world": points.get("bbox", {}),
        "warnings": list(warnings or []),
    }


def build_viewer_scene(
    *,
    dataset: str,
    run_id: str,
    sparse_ply: str | Path | None,
    trajectory: str | Path | None,
    alignment: str | Path,
    sfm_camera_path: str | Path | None,
    quality_timeline: str | Path | None,
    suggestions: str | Path | None,
    config: ExportViewerSceneConfig,
) -> tuple[dict, dict]:
    warnings: list[str] = []
    points = _empty_points(config)
    if sparse_ply:
        points = prepare_point_cloud(sparse_ply, alignment, config)
    else:
        warnings.append("未提供 sparse_ply，点云为空。")
    global_track: list[dict] = []
    if trajectory:
        global_track = build_global_sfm_track(trajectory, alignment, config)
    else:
        warnings.append("未提供 trajectory，global_sfm_track 为空。")
    anchored_track: list[dict] = []
    if sfm_camera_path:
        anchored_track = load_anchored_camera_path(sfm_camera_path, config)
    else:
        warnings.append("未提供 sfm_camera_path，anchored_camera_path 为空。")
    suggestions_rows = load_suggestions(suggestions) if suggestions else []
    scene = assemble_scene(
        dataset=dataset,
        run_id=run_id,
        points=points,
        global_track=global_track,
        anchored_track=anchored_track,
        suggestions=suggestions_rows,
        quality_timeline=quality_timeline,
        suggestions_path=suggestions,
    )
    return scene, scene_stats(scene, warnings)


def build_viewer_scene_report(inputs: Mapping[str, object], stats: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "# SfM Viewer Scene 导出报告",
            "",
            "## 输入文件",
            "",
            *[f"- {name}: `{value}`" for name, value in inputs.items()],
            "",
            "## 统计",
            "",
            f"- 点云原始数量：{stats.get('point_count_original', 0)}",
            f"- 点云导出数量：{stats.get('point_count_exported', 0)}",
            f"- 采样方式：{stats.get('point_sample_mode', 'none')}",
            f"- global_sfm_track 帧数：{stats.get('global_track_count', 0)}",
            f"- anchored_camera_path 帧数：{stats.get('anchored_track_count', 0)}",
            f"- suggestion 数量：{stats.get('suggestion_count', 0)}",
            "",
            "## 坐标约定",
            "",
            "- 点云和 global_sfm_track 只使用 global sim3。",
            "- anchored_camera_path 直接来自 sfm_camera_path.csv 的分段锚定结果。",
            "- 输出坐标统一为前端 web cad_world，pitch 已按前端约定取反。",
            "- 本阶段不修改相机轨迹，不做 refine。",
            "",
        ]
    )
