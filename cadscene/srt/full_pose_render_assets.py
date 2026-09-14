"""Bind full-pose telemetry to the shared terrain and calibrated renderer."""

import csv
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import numpy as np

from cadscene.srt.georeference import cad_raw_to_local_m, project_wgs84_to_cad_raw
from cadscene.terrain.context import build_terrain_context, write_terrain_context
from cadscene.terrain.tpkg import load_tpkg, merge_terrain_controls
from cadscene.terrain.height_reference import select_height_reference

from .full_pose import FullPoseBuildConfig, horizontal_fov_intrinsics


def publish_full_pose_render_assets(
    output: Path,
    trajectory: dict,
    config: FullPoseBuildConfig,
    payload: Mapping[str, object],
) -> None:
    """Use absolute camera Z with terrain; keep relative Z without usable terrain."""
    paths = payload.get("terrain_source_paths", [])
    fingerprints = payload.get("terrain_source_fingerprints", [])
    if not isinstance(paths, list) or not isinstance(fingerprints, list) or len(paths) != len(fingerprints):
        raise ValueError("terrain paths and fingerprints must be matching lists")
    controls_list, warnings = [], []

    def project(longitude, latitude):
        raw = project_wgs84_to_cad_raw(longitude, latitude, config.georeference)
        return cad_raw_to_local_m(raw, config.cad_origin_xy, config.cad_scale)

    for path, expected in zip(paths, fingerprints):
        try:
            controls = load_tpkg(Path(str(path)), project_lon_lat=project)
            if not controls.sources or controls.sources[0].source_id != expected:
                raise ValueError("terrain source fingerprint changed")
            controls_list.append(controls)
        except (OSError, ValueError) as exc:
            warnings.append(f"高程文件已排除 {Path(str(path)).name}: {exc}")
    controls = merge_terrain_controls(controls_list) if controls_list else None
    poses = [pose for pose in trajectory["poses"] if pose["registered"]]
    context = build_terrain_context(
        controls,
        np.asarray([pose["center"][:2] for pose in poses]),
        route_time_sec=[pose["source_pts"] * float(config.source_time_base) for pose in poses],
        cad_fingerprint=str(payload.get("cad_asset_fingerprint") or "unbound"),
        georeference_fingerprint=sha256(json.dumps(config.georeference.to_dict(), sort_keys=True).encode()).hexdigest(),
        warnings=warnings,
    )
    context = select_height_reference(context, controls, [pose.get("abs_alt") for pose in poses])
    if context.camera_height_datum_source == "srt_abs_alt_unverified":
        for pose in poses:
            pose["center"][2] = float(pose.get("smoothed_abs_alt", pose["abs_alt"])) + config.cad_z_offset_m
            if 'raw_center' in pose:
                pose['raw_center'][2] = float(pose['abs_alt']) + config.cad_z_offset_m
            pose["height_source"] = "abs_alt"
        trajectory["meta"]["height_source_counts"] = {"abs_alt": len(poses)}
        # All consumers must see the same Z, including the exported raw CSV.
        path_csv = output / "camera_path_full_pose.csv"
        with path_csv.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        by_frame = {int(pose["frame_index"]): pose for pose in poses}
        for row in rows:
            pose = by_frame.get(int(row["frame_index"]))
            if pose is not None:
                row["camera_z"] = pose["center"][2]
        with path_csv.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    write_terrain_context(output, context, controls)
    intrinsics = horizontal_fov_intrinsics(trajectory["width"], trajectory["height"], config.horizontal_fov_deg)
    focal, _, cx, cy = intrinsics["params"]
    calibration = {
        "schema_version": 1, "model": "PINHOLE",
        "source_size": [trajectory["width"], trajectory["height"]],
        "focal_px": focal, "cx_px": cx, "cy_px": cy, "k1": 0.0, "k2": 0.0,
        "intrinsics_source": "user_horizontal_fov",
        "distortion_source": "unknown_assumed_zero",
        "horizontal_fov_deg": config.horizontal_fov_deg,
    }
    (output / "camera_calibration.json").write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    trajectory["meta"].update({
        **context.to_dict(),
        "camera_model": "PINHOLE", "camera_focal_px": focal,
        "camera_principal_point_px": [cx, cy], "camera_radial_distortion": [0.0, 0.0],
        "intrinsics_source": calibration["intrinsics_source"],
        "distortion_source": calibration["distortion_source"],
        "warnings": list(dict.fromkeys([*trajectory["meta"].get("warnings", []), *context.warnings])),
    })
    (output / "camera_trajectory_full_pose.json").write_text(json.dumps(trajectory, ensure_ascii=False, indent=2), encoding="utf-8")
    diagnostics_path = output / "georeference_diagnostics.json"
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    diagnostics.update(context.to_dict())
    diagnostics["height_source_counts"] = trajectory["meta"].get("height_source_counts", {})
    diagnostics["vertical_validation"] = {
        "status": context.camera_height_datum_source,
        "cad_z_offset_m": config.cad_z_offset_m,
        "height_source_counts": diagnostics["height_source_counts"],
        "warnings": list(context.warnings),
    }
    diagnostics["warnings"] = trajectory["meta"]["warnings"]
    diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "full_pose_report.md").open("a", encoding="utf-8") as report:
        report.write(f"\n- 高程文件：{len(context.source_fingerprints)}；覆盖率：{context.coverage_fraction:.1%}\n")
        report.write("- 内参：用户水平 FOV；畸变未知，按零处理。\n")
        report.write(f"- 最终相机高度来源：{diagnostics['height_source_counts']}；垂直策略：{context.camera_height_datum_source}\n")
        report.write("\n".join(f"- {warning}" for warning in context.warnings) + "\n")
