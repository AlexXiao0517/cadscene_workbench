from __future__ import annotations

import csv
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from cadscene.alignment.aligner import AlignmentConfig, aligned_state_at_frame
from cadscene.alignment.keyframes import confirmed_keyframes
from cadscene.core.camera import CameraState
from cadscene.core.coordinates import web_camera_to_python_state
from cadscene.core.io import read_json
from cadscene.sfm.trajectory import load_sfm_trajectory
from cadscene.viewer.export_scene import load_sim3_from_alignment


def _angle_diff(a: float, b: float) -> float:
    return (float(a) - float(b) + 180.0) % 360.0 - 180.0


def load_anchored_rows(path: str | Path) -> dict[int, CameraState]:
    out: dict[int, CameraState] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            out[int(float(row["frame_index"]))] = CameraState(
                camera_x=float(row["camera_x"]),
                camera_y=float(row["camera_y"]),
                camera_z=float(row["camera_z"]),
                yaw_deg=float(row.get("yaw", 0.0)),
                pitch_deg=float(row.get("pitch", 0.0)),
                roll_deg=float(row.get("roll", 0.0)),
                fov_deg=float(row.get("fov", 70.0)),
            )
    return out


def _nearest_anchor(rows: Mapping[int, CameraState], frame: int) -> CameraState | None:
    if not rows:
        return None
    nearest = min(rows, key=lambda f: abs(f - frame))
    return rows[nearest]


def keyframe_pose_residuals(web_camera_track, trajectory, alignment, sfm_camera_path, cad_scale: float, origin_xy) -> list[dict]:
    track = read_json(web_camera_track) if not isinstance(web_camera_track, dict) else web_camera_track
    traj = load_sfm_trajectory(trajectory)
    sim3 = load_sim3_from_alignment(alignment)
    config = AlignmentConfig(cad_scale=cad_scale, origin_xy=tuple(origin_xy), fov_from="trajectory")
    anchored = load_anchored_rows(sfm_camera_path)
    rows: list[dict] = []
    for item in confirmed_keyframes(track):
        frame = int(item.get("frame", 0))
        manual = web_camera_to_python_state(item["camera"], origin_xy, cad_scale)
        global_state = aligned_state_at_frame(frame, traj, sim3, anchored=None, config=config)
        anch = _nearest_anchor(anchored, frame) or CameraState()

        def diff(a: CameraState, b: CameraState, prefix: str) -> dict:
            dx = a.camera_x - b.camera_x
            dy = a.camera_y - b.camera_y
            dz = a.camera_z - b.camera_z
            return {
                f"{prefix}_dx": dx,
                f"{prefix}_dy": dy,
                f"{prefix}_dz": dz,
                f"{prefix}_dist": float(np.linalg.norm([dx, dy, dz])),
                f"{prefix}_yaw": _angle_diff(a.yaw_deg, b.yaw_deg),
                f"{prefix}_pitch": _angle_diff(a.pitch_deg, b.pitch_deg),
                f"{prefix}_roll": _angle_diff(a.roll_deg, b.roll_deg),
            }

        row = {
            "frame_index": frame,
            "source": item.get("source", ""),
            "manual_x": manual.camera_x,
            "manual_y": manual.camera_y,
            "manual_z": manual.camera_z,
            "global_sfm_x": global_state.camera_x,
            "global_sfm_y": global_state.camera_y,
            "global_sfm_z": global_state.camera_z,
            "anchored_x": anch.camera_x,
            "anchored_y": anch.camera_y,
            "anchored_z": anch.camera_z,
        }
        row.update(diff(manual, global_state, "manual_vs_global"))
        row.update(diff(manual, anch, "manual_vs_anchored"))
        rows.append(row)
    return rows


def pose_compensation_warning(residual_rows: Sequence[Mapping[str, object]], profile_rows: Sequence[Mapping[str, object]]) -> dict:
    reasons: list[str] = []
    dz = np.asarray([float(row.get("manual_vs_global_dz", 0.0)) for row in residual_rows], dtype=np.float64)
    if len(dz) and float(np.median(np.abs(dz))) > 1.0:
        reasons.append("median manual_vs_global_z residual > 1.0m")
    if len(dz) >= 2 and profile_rows:
        prof = np.asarray([float(row.get("median_z") or 0.0) for row in profile_rows[: len(dz)]], dtype=np.float64)
        if len(prof) == len(dz) and np.std(prof) > 1e-9 and np.std(dz) > 1e-9:
            corr = float(np.corrcoef(prof, dz)[0, 1])
            if abs(corr) > 0.5:
                reasons.append("keyframe z residual follows road surface trend")
    return {"pose_compensation_warning": bool(reasons), "pose_compensation_reasons": reasons}


def camera_z_profile(trajectory, alignment, sfm_camera_path, web_camera_track, cad_scale: float, origin_xy, profile_rows: Sequence[Mapping[str, object]] | None = None) -> list[dict]:
    traj = load_sfm_trajectory(trajectory)
    sim3 = load_sim3_from_alignment(alignment)
    config = AlignmentConfig(cad_scale=cad_scale, origin_xy=tuple(origin_xy), fov_from="trajectory")
    anchored = load_anchored_rows(sfm_camera_path)
    track = read_json(web_camera_track) if web_camera_track else {"keyframes": []}
    manual = {int(item.get("frame", 0)): web_camera_to_python_state(item["camera"], origin_xy, cad_scale).camera_z for item in confirmed_keyframes(track)}
    road_z = ""
    if profile_rows:
        vals = [float(row.get("median_z") or 0.0) for row in profile_rows if row.get("median_z") != ""]
        road_z = float(np.median(vals)) if vals else ""
    rows: list[dict] = []
    for frame in [int(v) for v in traj.frames.tolist()]:
        global_state = aligned_state_at_frame(frame, traj, sim3, anchored=None, config=config)
        anch = _nearest_anchor(anchored, frame)
        rows.append(
            {
                "frame_index": frame,
                "global_sfm_z": global_state.camera_z,
                "anchored_z": "" if anch is None else anch.camera_z,
                "manual_z_if_available": manual.get(frame, ""),
                "road_surface_z_if_available": road_z,
            }
        )
    return rows
