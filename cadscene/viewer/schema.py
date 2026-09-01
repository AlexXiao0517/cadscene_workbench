from __future__ import annotations

SCHEMA_VERSION = "cadscene_sfm_viewer_scene_v1"


def validate_viewer_scene(scene: dict) -> None:
    if scene.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("sfm_viewer_scene schema_version 不正确。")
    for key in ("meta", "points", "tracks", "suggestions", "quality"):
        if key not in scene:
            raise ValueError(f"sfm_viewer_scene 缺少字段：{key}")
    if scene["meta"].get("coordinate_system") != "web_cad_world":
        raise ValueError("meta.coordinate_system 必须是 web_cad_world。")
    if scene["meta"].get("rgb_range") != "0_255":
        raise ValueError("meta.rgb_range 必须是 0_255。")
    points = scene["points"]
    for key in ("count_original", "count_exported", "sample_mode", "voxel_size", "has_rgb", "bbox", "data"):
        if key not in points:
            raise ValueError(f"points 缺少字段：{key}")
    if points.get("rgb_range") != "0_255":
        raise ValueError("points.rgb_range 必须是 0_255。")
    tracks = scene["tracks"]
    if "global_sfm_track" not in tracks or "anchored_camera_path" not in tracks:
        raise ValueError("tracks 必须包含 global_sfm_track 和 anchored_camera_path。")
    for row in tracks["global_sfm_track"]:
        if row.get("source") not in {"global_sim3_sfm", "metric_direct_srt"}:
            raise ValueError(
                "global_sfm_track source 必须是 global_sim3_sfm 或 metric_direct_srt。"
            )
        _validate_track_row(row)
    for row in tracks["anchored_camera_path"]:
        if row.get("source") != "segment_anchor_path":
            raise ValueError("anchored_camera_path source 必须是 segment_anchor_path。")
        _validate_track_row(row)


def _validate_track_row(row: dict) -> None:
    if "frame_index" not in row or "camera" not in row:
        raise ValueError("track row 必须包含 frame_index 和 camera。")
    camera = row["camera"]
    for key in ("x", "y", "z", "yaw", "pitch", "roll", "fov"):
        if key not in camera:
            raise ValueError(f"camera 缺少字段：{key}")
