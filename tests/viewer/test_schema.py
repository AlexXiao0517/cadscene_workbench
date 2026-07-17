from __future__ import annotations

from cadscene.viewer.schema import SCHEMA_VERSION, validate_viewer_scene


def test_viewer_scene_schema_required_fields_and_sources() -> None:
    scene = {
        "schema_version": SCHEMA_VERSION,
        "meta": {"coordinate_system": "web_cad_world", "generated_by": "cadscene.export_viewer_scene", "rgb_range": "0_255"},
        "points": {"count_original": 0, "count_exported": 0, "sample_mode": "none", "voxel_size": 0.5, "has_rgb": False, "rgb_range": "0_255", "bbox": {"min": [0, 0, 0], "max": [0, 0, 0]}, "data": []},
        "tracks": {
            "global_sfm_track": [{"frame_index": 0, "camera": {"x": 0, "y": 0, "z": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}, "source": "global_sim3_sfm"}],
            "anchored_camera_path": [{"frame_index": 0, "camera": {"x": 0, "y": 0, "z": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}, "source": "segment_anchor_path"}],
        },
        "suggestions": [],
        "quality": {"available": False, "quality_timeline_ref": "", "suggestions_ref": ""},
    }

    validate_viewer_scene(scene)
    assert scene["schema_version"] == "cadscene_sfm_viewer_scene_v1"
    assert scene["meta"]["coordinate_system"] == "web_cad_world"


def test_viewer_scene_schema_rejects_wrong_source() -> None:
    scene = {
        "schema_version": SCHEMA_VERSION,
        "meta": {"coordinate_system": "web_cad_world", "rgb_range": "0_255"},
        "points": {"count_original": 0, "count_exported": 0, "sample_mode": "none", "voxel_size": 0.5, "has_rgb": False, "rgb_range": "0_255", "bbox": {"min": [0, 0, 0], "max": [0, 0, 0]}, "data": []},
        "tracks": {"global_sfm_track": [{"frame_index": 0, "camera": {}, "source": "segment_anchor_path"}], "anchored_camera_path": []},
        "suggestions": [],
        "quality": {"available": False},
    }

    try:
        validate_viewer_scene(scene)
    except ValueError as exc:
        assert "global_sfm_track" in str(exc)
    else:
        raise AssertionError("validate_viewer_scene should reject wrong global track source")
