from __future__ import annotations

import pytest

from cadscene.alignment.keyframe_schema import normalize_camera_track


def _camera(**overrides):
    camera = {"x": 10.0, "y": 20.0, "z": 30.0, "yaw": 15.0, "pitch": -10.0, "roll": 2.0, "fov": 75.0}
    camera.update(overrides)
    return camera


def test_schema_v2_normalizes_explicit_pose_metadata():
    track = {
        "schema_version": 2,
        "coordinate_system": "web_cad_world",
        "pose_convention": {"angle_unit": "degree", "pitch_convention": "frontend_pitch"},
        "keyframes": [{
            "frame": 12, "source_frame_index": 12, "time": 0.4, "pts_time_sec": 0.401,
            "source": "manual_anchor", "camera": _camera(),
            "orientation_metadata": {"orientation_source": "manual", "orientation_confirmed": True,
                                     "yaw_confirmed": True, "pitch_confirmed": True, "roll_confirmed": False},
            "prior": {"enabled": True, "direction_type": "camera_forward", "solver_role": "solve", "quality": "unverified"},
        }],
    }

    normalized = normalize_camera_track(track)

    assert normalized["schema_version"] == 2
    assert normalized["coordinate_system"] == "web_cad_world"
    assert normalized["keyframes"][0]["source_frame_index"] == 12
    assert normalized["keyframes"][0]["pts_time_sec"] == pytest.approx(0.401)
    assert normalized["keyframes"][0]["prior"]["solver_role"] == "solve"


def test_legacy_track_remains_readable_but_is_not_a_qualified_prior():
    legacy = {"keyframes": [{"frame": 7, "time": 0.233, "source": "manual_keyframe", "camera": _camera()}]}

    normalized = normalize_camera_track(legacy)
    frame = normalized["keyframes"][0]

    assert normalized["schema_version"] == 1
    assert normalized["coordinate_system"] == "web_cad_world_legacy"
    assert frame["source_frame_index"] == 7
    assert frame["frame_mapping_source"] == "legacy_inferred"
    assert frame["timestamp_source"] == "legacy_inferred"
    assert frame["orientation_metadata"]["orientation_confirmed"] is False
    assert frame["orientation_metadata"]["roll_confirmed"] is False
    assert frame["prior"]["solver_role"] == "none"


def test_schema_v2_rejects_non_finite_camera_values():
    track = {
        "schema_version": 2, "coordinate_system": "web_cad_world", "pose_convention": {},
        "keyframes": [{"source_frame_index": 1, "pts_time_sec": 0.0, "camera": _camera(yaw=float("nan"))}],
    }

    with pytest.raises(ValueError, match="finite"):
        normalize_camera_track(track)
