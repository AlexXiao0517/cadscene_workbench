from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from cadscene.cad.centerline import CenterlineModel, camera_state_cad_xy, project_station_lateral
from cadscene.cad.loader import CadBundle
from cadscene.core.camera import CameraState
from cadscene.core.coordinates import web_camera_to_python_state

CONFIRMED_KEYFRAME_SOURCES = frozenset(
    {
        "manual_keyframe",
        "confirmed_keyframe",
        "manual",
        "confirmed",
        "manual_anchor",
        "manual_corrected",
        "scene_boundary_anchor",
        "scene_overlap_anchor",
    }
)


@dataclass(frozen=True)
class KeyframeAnchor:
    frame_index: int
    state: CameraState
    camera_s: float
    camera_lateral_d: float
    source: str = "manual_keyframe"

    def to_dict(self) -> dict:
        return {
            "frame": int(self.frame_index),
            "camera_x": float(self.state.camera_x),
            "camera_y": float(self.state.camera_y),
            "camera_z": float(self.state.camera_z),
            "yaw": float(self.state.yaw_deg),
            "pitch": float(self.state.pitch_deg),
            "roll": float(self.state.roll_deg),
            "fov": float(self.state.fov_deg),
            "camera_s": float(self.camera_s),
            "camera_lateral_d": float(self.camera_lateral_d),
            "source": self.source,
        }


def load_web_camera_track(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def is_confirmed_keyframe(item: object) -> bool:
    """只接受用户明确保存过的人工关键帧，排除 Viewer 初始预览位姿。"""
    return (
        isinstance(item, Mapping)
        and isinstance(item.get("camera"), Mapping)
        and str(item.get("source") or "") in CONFIRMED_KEYFRAME_SOURCES
    )


def confirmed_keyframes(track: dict) -> list[dict]:
    candidates = [
        item
        for item in track.get("keyframes", [])
        if isinstance(item, Mapping) and isinstance(item.get("camera"), Mapping)
    ]
    explicit = [item for item in candidates if is_confirmed_keyframe(item)]
    if explicit:
        keyframes = explicit
    else:
        legacy = [item for item in candidates if not str(item.get("source") or "")]
        keyframes = legacy if len(legacy) >= 2 else []
    keyframes.sort(key=lambda row: int(row.get("frame", 0)))
    return keyframes


def build_anchor_summary(
    keyframes: Iterable[dict],
    bundle: CadBundle,
    center: CenterlineModel,
    cad_scale: float,
) -> list[KeyframeAnchor]:
    anchors: list[KeyframeAnchor] = []
    for keyframe in keyframes:
        camera = keyframe.get("camera") or {}
        if not all(key in camera for key in ("x", "y", "z")):
            continue
        state = web_camera_to_python_state(camera, bundle.origin_xy, cad_scale)
        camera_s, lateral_d, _idx = project_station_lateral(center, camera_state_cad_xy(bundle, state))
        anchors.append(
            KeyframeAnchor(
                frame_index=int(keyframe.get("frame", 0)),
                state=state,
                camera_s=float(camera_s),
                camera_lateral_d=float(lateral_d),
                source=str(keyframe.get("source") or "manual_keyframe"),
            )
        )
    anchors.sort(key=lambda row: row.frame_index)
    if not anchors:
        raise RuntimeError("camera_track.json 中没有可用人工关键帧。")
    return anchors


def anchor_segments(anchors: Sequence[KeyframeAnchor]) -> list[dict]:
    segments: list[dict] = []
    for prev, curr in zip(anchors[:-1], anchors[1:]):
        df = curr.frame_index - prev.frame_index
        ds = curr.camera_s - prev.camera_s
        segments.append(
            {
                "frame_a": prev.frame_index,
                "frame_b": curr.frame_index,
                "station_delta": float(ds),
                "frame_delta": int(df),
                "station_velocity_mpf": float(ds / df) if df else 0.0,
            }
        )
    return segments
