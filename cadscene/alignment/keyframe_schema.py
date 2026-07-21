"""Versioned, read-only normalization for manual camera-track records.

The existing viewer continues to write its legacy JSON payload.  This module
creates a normalized in-memory view for consumers that need explicit pose
provenance; it never rewrites a camera-track file.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping


CAMERA_TRACK_SCHEMA_VERSION = 2

DEFAULT_POSE_CONVENTION: dict[str, Any] = {
    "position_unit": "web_unit",
    "angle_unit": "degree",
    "world_axes": {"x": "cad_x", "y": "cad_y", "z": "up"},
    "camera_axes": {"x": "right", "y": "down", "z": "forward"},
    "rotation_representation": "yaw_pitch_roll",
    "rotation_semantics": "camera_to_world",
    "pitch_convention": "frontend_pitch",
    "conversion_to_backend": "web_camera_to_python_state_v1",
}


def _finite(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _orientation_source(keyframe: Mapping[str, Any]) -> str:
    if str(keyframe.get("source") or "") == "algorithm_prediction":
        return "algorithm"
    return "manual"


def _normalized_camera(camera: Mapping[str, Any]) -> dict[str, float]:
    required = ("x", "y", "z", "yaw", "pitch", "roll", "fov")
    missing = [name for name in required if name not in camera]
    if missing:
        raise ValueError(f"camera is missing required fields: {', '.join(missing)}")
    return {name: _finite(camera[name], f"camera.{name}") for name in required}


def _legacy_keyframe(raw: Mapping[str, Any]) -> dict[str, Any]:
    frame = int(raw.get("frame", 0))
    timestamp = _finite(raw.get("time", 0.0), "time")
    return {
        **deepcopy(dict(raw)),
        "frame": frame,
        "source_frame_index": frame,
        "frame_mapping_source": "legacy_inferred",
        "time": timestamp,
        "pts_time_sec": timestamp,
        "timestamp_source": "legacy_inferred",
        "camera": _normalized_camera(dict(raw.get("camera") or {})),
        "orientation_metadata": {
            "orientation_source": _orientation_source(raw),
            "orientation_confirmed": False,
            "yaw_confirmed": False,
            "pitch_confirmed": False,
            "roll_confirmed": False,
            "projection_checked": False,
            "projection_residual_px": None,
        },
        "prior": {
            "enabled": False,
            "direction_type": None,
            "solver_role": "none",
            "quality": "unverified",
        },
    }


def _v2_keyframe(raw: Mapping[str, Any]) -> dict[str, Any]:
    if "source_frame_index" not in raw or "pts_time_sec" not in raw:
        raise ValueError("schema v2 keyframe requires source_frame_index and pts_time_sec")
    metadata = dict(raw.get("orientation_metadata") or {})
    prior = dict(raw.get("prior") or {})
    source_frame_index = int(raw["source_frame_index"])
    if source_frame_index < 0:
        raise ValueError("source_frame_index must be non-negative")
    pts_time_sec = _finite(raw["pts_time_sec"], "pts_time_sec")
    projection_residual = metadata.get("projection_residual_px")
    if projection_residual is not None:
        projection_residual = _finite(projection_residual, "projection_residual_px")
    return {
        **deepcopy(dict(raw)),
        "frame": int(raw.get("frame", source_frame_index)),
        "source_frame_index": source_frame_index,
        "frame_mapping_source": str(raw.get("frame_mapping_source") or "explicit"),
        "time": _finite(raw.get("time", pts_time_sec), "time"),
        "pts_time_sec": pts_time_sec,
        "timestamp_source": str(raw.get("timestamp_source") or "explicit"),
        "camera": _normalized_camera(dict(raw.get("camera") or {})),
        "orientation_metadata": {
            "orientation_source": str(metadata.get("orientation_source") or _orientation_source(raw)),
            "orientation_confirmed": bool(metadata.get("orientation_confirmed", False)),
            "yaw_confirmed": bool(metadata.get("yaw_confirmed", False)),
            "pitch_confirmed": bool(metadata.get("pitch_confirmed", False)),
            "roll_confirmed": bool(metadata.get("roll_confirmed", False)),
            "projection_checked": bool(metadata.get("projection_checked", False)),
            "projection_residual_px": projection_residual,
        },
        "prior": {
            "enabled": bool(prior.get("enabled", False)),
            "direction_type": prior.get("direction_type"),
            "solver_role": str(prior.get("solver_role") or "none"),
            "quality": str(prior.get("quality") or "unverified"),
        },
    }


def normalize_camera_track(track: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized read-only view of a legacy or v2 camera track."""

    raw = dict(track)
    frames = raw.get("keyframes")
    if not isinstance(frames, list):
        raise ValueError("camera track keyframes must be a list")
    schema_version = raw.get("schema_version")
    is_v2 = schema_version == CAMERA_TRACK_SCHEMA_VERSION
    if schema_version is not None and not is_v2:
        # Existing prediction tracks use a string schema version and are legacy
        # records from the perspective of the prior package.
        is_v2 = False
    convention = deepcopy(DEFAULT_POSE_CONVENTION)
    convention.update(dict(raw.get("pose_convention") or {}))
    return {
        **deepcopy(raw),
        "schema_version": CAMERA_TRACK_SCHEMA_VERSION if is_v2 else 1,
        "coordinate_system": str(raw.get("coordinate_system") or ("web_cad_world" if is_v2 else "web_cad_world_legacy")),
        "pose_convention": convention,
        "keyframes": [
            _v2_keyframe(frame) if is_v2 else _legacy_keyframe(frame)
            for frame in frames
        ],
    }
