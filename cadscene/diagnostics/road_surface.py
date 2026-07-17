from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from cadscene.cad.centerline import CenterlineModel, project_station_lateral
from cadscene.cad.loader import load_cad_bundle
from cadscene.core.coordinates import cad_meters_to_web_camera
from cadscene.diagnostics.geometry import error_stats
from cadscene.sfm.pointcloud import load_ply
from cadscene.viewer.export_scene import load_sim3_from_alignment, sample_point_indices


def load_centerline(
    cad_dir: str | Path,
    origin_xy: tuple[float, float] | list[float] | None = None,
    cad_scale: float | None = None,
) -> CenterlineModel | None:
    bundle = load_cad_bundle(cad_dir, origin_xy=origin_xy, cad_scale=cad_scale)
    if not bundle.centers:
        return None
    points = np.vstack([line.points for line in bundle.centers if len(line.points)])
    return CenterlineModel.from_points(points) if len(points) >= 2 else None


def project_to_centerline(points_xy: np.ndarray, centerline: np.ndarray | CenterlineModel) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    center = centerline if isinstance(centerline, CenterlineModel) else CenterlineModel.from_points(centerline)
    station, lateral, nearest = [], [], []
    for point in np.asarray(points_xy, dtype=np.float64).reshape(-1, 2):
        s, d, idx = project_station_lateral(center, point)
        station.append(s)
        lateral.append(d)
        nearest.append(idx)
    return np.asarray(station), np.asarray(lateral), np.asarray(nearest, dtype=np.int64)


def extract_road_points_corridor(points_cad_m: np.ndarray, lateral: np.ndarray, road_corridor_width: float = 15.0) -> np.ndarray:
    points = np.asarray(points_cad_m, dtype=np.float64).reshape(-1, 3)
    if len(points) == 0:
        return np.zeros(0, dtype=bool)
    return np.abs(np.asarray(lateral, dtype=np.float64)) <= float(road_corridor_width)


def road_surface_profile(points_cad_m: np.ndarray, station: np.ndarray, bin_m: float = 20.0) -> list[dict]:
    pts = np.asarray(points_cad_m, dtype=np.float64).reshape(-1, 3)
    s = np.asarray(station, dtype=np.float64).reshape(-1)
    if len(pts) == 0 or len(s) == 0:
        return []
    start = float(np.floor(s.min() / bin_m) * bin_m)
    end = float(np.ceil(s.max() / bin_m) * bin_m)
    if end <= start:
        end = start + float(bin_m)
    rows: list[dict] = []
    lo = start
    while lo < end:
        hi = lo + float(bin_m)
        mask = (s >= lo) & (s < hi if hi < end else s <= hi)
        z = pts[mask, 2]
        if len(z):
            p10, p90 = np.percentile(z, [10, 90])
            q25, q75 = np.percentile(z, [25, 75])
            med = float(np.median(z))
            rows.append(
                {
                    "station_start": lo,
                    "station_end": hi,
                    "station_mid": (lo + hi) / 2.0,
                    "point_count": int(len(z)),
                    "support_score": float(min(1.0, len(z) / 20.0)),
                    "median_z": med,
                    "mean_z": float(np.mean(z)),
                    "p10_z": float(p10),
                    "p90_z": float(p90),
                    "z_iqr": float(q75 - q25),
                    "min_z": float(z.min()),
                    "max_z": float(z.max()),
                    "flat_abs_error_median": abs(med),
                    "flat_abs_error_p90": float(np.percentile(np.abs(z), 90)),
                    "global_plane_abs_error_median": "",
                    "profile_abs_error_median": "",
                }
            )
        lo = hi
    return rows


def flat_plane_error(z_values: Sequence[float]) -> dict:
    return error_stats(np.asarray(z_values, dtype=np.float64))


def fit_global_plane(points_cad_m: np.ndarray) -> dict:
    pts = np.asarray(points_cad_m, dtype=np.float64).reshape(-1, 3)
    if len(pts) < 3:
        return {"a": 0.0, "b": 0.0, "c": 0.0, **error_stats([])}
    A = np.column_stack([pts[:, 0], pts[:, 1], np.ones(len(pts))])
    coef, *_ = np.linalg.lstsq(A, pts[:, 2], rcond=None)
    residual = pts[:, 2] - (A @ coef)
    return {"a": float(round(coef[0], 12)), "b": float(round(coef[1], 12)), "c": float(round(coef[2], 12)), **error_stats(residual)}


def profile_surface_error(points_cad_m: np.ndarray, station: np.ndarray, bin_m: float = 20.0) -> dict:
    pts = np.asarray(points_cad_m, dtype=np.float64).reshape(-1, 3)
    s = np.asarray(station, dtype=np.float64).reshape(-1)
    if len(pts) == 0:
        return {**error_stats([]), "profile_bins_used": 0}
    rows = [row for row in road_surface_profile(pts, s, bin_m) if row["point_count"] > 0]
    centers = np.asarray([row["station_mid"] for row in rows], dtype=np.float64)
    medians = np.asarray([row["median_z"] for row in rows], dtype=np.float64)
    if len(centers) == 0:
        return {**error_stats([]), "profile_bins_used": 0}
    pred = np.interp(s, centers, medians)
    return {**error_stats(pts[:, 2] - pred), "profile_bins_used": int(len(centers))}


def classify_surface(road_point_count: int, flat_rmse: float, flat_p90: float, plane_rmse: float, profile_rmse: float) -> dict:
    if road_point_count < 3:
        status = "insufficient_points"
    elif flat_rmse <= 0.5 and flat_p90 <= 1.0:
        status = "likely_flat"
    elif plane_rmse < flat_rmse * 0.7 and profile_rmse >= plane_rmse * 0.8:
        status = "globally_tilted"
    elif profile_rmse < flat_rmse * 0.7:
        status = "non_flat_along_station"
    else:
        status = "likely_flat" if flat_rmse < 1.0 else "non_flat_along_station"
    return {"road_flatness_status": status}


def summarize_geometry(
    *,
    road_point_count: int,
    road_point_ratio: float,
    flat: Mapping[str, float],
    plane: Mapping[str, float],
    profile: Mapping[str, float],
    pose_warning: Mapping[str, object],
) -> dict:
    flat_rmse = float(flat.get("rmse", 0.0))
    flat_p90 = float(flat.get("p90_abs", 0.0))
    plane_rmse = float(plane.get("rmse", 0.0))
    profile_rmse = float(profile.get("rmse", 0.0))
    flatness = classify_surface(road_point_count, flat_rmse, flat_p90, plane_rmse, profile_rmse)["road_flatness_status"]
    reliability = "insufficient" if road_point_count < 3 else "high" if road_point_ratio > 0.4 and flat_rmse < 1.0 else "medium" if road_point_ratio > 0.1 else "low"
    if reliability == "insufficient":
        flat_assumption = "insufficient"
    elif flat_rmse <= 0.5 and flat_p90 <= 1.0:
        flat_assumption = "acceptable"
    elif plane_rmse < flat_rmse * 0.7 or flat_rmse > 2.0:
        flat_assumption = "likely_wrong"
    else:
        flat_assumption = "questionable"
    sfm_ref = "unsuitable" if reliability == "insufficient" else "suitable" if reliability in {"high", "medium"} and flat_rmse < 2.0 else "use_with_caution"
    pose_flag = bool(pose_warning.get("pose_compensation_warning", False))
    if reliability == "insufficient":
        next_step = "need_more_sfm_points_or_segmentation"
    elif pose_flag:
        next_step = "inspect_keyframe_pose_bias"
    elif flat_assumption == "acceptable":
        next_step = "keep_flat_cad"
    elif flatness == "globally_tilted":
        next_step = "try_global_tilt_plane"
    else:
        next_step = "try_station_height_profile"
    return {
        "road_point_count": int(road_point_count),
        "road_point_ratio": float(road_point_ratio),
        "flat_plane": dict(flat),
        "global_plane": dict(plane),
        "station_profile": dict(profile),
        "conclusions": {
            "road_flatness_status": flatness,
            "road_surface_reliability": reliability,
            "cad_flat_plane_assumption": flat_assumption,
            "sfm_as_alignment_reference": sfm_ref,
            "next_step_recommendation": next_step,
            "pose_compensation_warning": pose_flag,
            "pose_compensation_reasons": list(pose_warning.get("pose_compensation_reasons", [])),
        },
    }


def sfm_surface_quality(profile_rows: Sequence[Mapping[str, object]]) -> list[dict]:
    rows = []
    for row in profile_rows:
        support = float(row.get("support_score") or 0.0)
        flat_err = float(row.get("flat_abs_error_median") or 0.0)
        iqr = float(row.get("z_iqr") or 0.0)
        risk = float(np.clip(max(1.0 - support, flat_err / 2.0, iqr / 1.0), 0.0, 1.0))
        level = "high" if risk >= 0.65 else "medium" if risk >= 0.35 else "low"
        rows.append(
            {
                "station_mid": row.get("station_mid", 0.0),
                "support_score": support,
                "flat_error_score": float(np.clip(flat_err / 2.0, 0.0, 1.0)),
                "plane_error_score": "",
                "profile_error_score": "",
                "z_iqr_score": float(np.clip(iqr, 0.0, 1.0)),
                "surface_risk_score": risk,
                "risk_level": level,
                "reason_codes": "low_station_support" if support < 0.5 else "",
            }
        )
    return rows


def transform_pointcloud_to_cad(sparse_ply: str | Path, alignment: str | Path, max_points: int = 120000, sample_mode: str = "voxel", voxel_size: float = 0.5) -> tuple[np.ndarray, np.ndarray | None, int]:
    points_sfm, colors = load_ply(sparse_ply)
    sim3 = load_sim3_from_alignment(alignment)
    points_cad = sim3.apply(points_sfm)
    idx = sample_point_indices(points_cad, max_points, sample_mode, voxel_size)
    return points_cad[idx], (colors[idx] if colors is not None else None), int(len(points_sfm))


def write_ply_cadworld(path: str | Path, points: np.ndarray, colors: np.ndarray | None = None) -> None:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    cols = colors if colors is not None else np.zeros((len(pts), 3), dtype=np.uint8)
    lines = ["ply", "format ascii 1.0", f"element vertex {len(pts)}", "property float x", "property float y", "property float z", "property uchar red", "property uchar green", "property uchar blue", "end_header"]
    for p, c in zip(pts, cols):
        lines.append(f"{p[0]} {p[1]} {p[2]} {int(c[0])} {int(c[1])} {int(c[2])}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def viewer_diagnostics_scene(points_cad: np.ndarray, profile_rows: Sequence[dict], residual_rows: Sequence[dict], origin_xy, cad_scale, warnings: Sequence[str]) -> dict:
    idx = sample_point_indices(points_cad, 1000, "uniform", 1.0)
    road_points = []
    for p in np.asarray(points_cad, dtype=np.float64)[idx]:
        cam = cad_meters_to_web_camera(p, origin_xy, cad_scale)
        road_points.append([cam["x"], cam["y"], cam["z"]])
    return {"schema_version": "cadscene_road_surface_diagnostics_v1", "coordinate_system": "web_cad_world", "road_points": road_points, "profile": list(profile_rows), "keyframe_residuals": list(residual_rows), "warnings": list(warnings)}
