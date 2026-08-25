from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import math
from typing import Collection, Mapping, Sequence

from cadscene.video_analysis.pts import DecodedFrameIndex


class SceneBridgeUnavailable(ValueError):
    """The requested clips cannot establish an exact two-frame scene bridge."""


@dataclass(frozen=True)
class FrameMapEntry:
    local_ordinal: int
    source_ordinal: int
    source_pts: int


@dataclass(frozen=True)
class FrameMap:
    clip_id: str
    source_start_pts: int
    source_end_pts_exclusive: int
    time_base: Fraction
    frames: tuple[FrameMapEntry, ...]

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("frame map clip_id must not be empty")
        if self.time_base <= 0:
            raise ValueError("frame map time base must be positive")
        if self.source_end_pts_exclusive <= self.source_start_pts:
            raise ValueError("frame map interval must be increasing")
        if not self.frames:
            raise ValueError("frame map must contain frames")
        for index, frame in enumerate(self.frames):
            if frame.local_ordinal != index:
                raise ValueError("frame map local ordinals must be contiguous")
            if not self.source_start_pts <= frame.source_pts < self.source_end_pts_exclusive:
                raise ValueError("frame map PTS escapes its interval")
            if index and frame.source_pts <= self.frames[index - 1].source_pts:
                raise ValueError("frame map PTS must be unique and increasing")

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object], *, clip_id: str | None = None
    ) -> FrameMap:
        selected: Mapping[str, object] = payload
        raw_clips = payload.get("clips")
        if isinstance(raw_clips, list):
            candidates = [item for item in raw_clips if isinstance(item, Mapping)]
            if clip_id is None:
                if len(candidates) != 1:
                    raise ValueError("clip_id is required for a multi-clip frame map")
                selected = candidates[0]
            else:
                match = next(
                    (item for item in candidates if item.get("clip_id") == clip_id),
                    None,
                )
                if match is None:
                    raise ValueError(f"frame map does not contain clip_id: {clip_id}")
                selected = match
        resolved_clip_id = selected.get("clip_id", clip_id)
        if not isinstance(resolved_clip_id, str) or not resolved_clip_id:
            raise ValueError("frame map clip_id is missing")
        if clip_id is not None and resolved_clip_id != clip_id:
            raise ValueError("frame map clip_id does not match the requested clip")
        raw_time_base = payload.get("source_time_base")
        if not isinstance(raw_time_base, Mapping):
            raw_time_base = selected.get("source_time_base")
        if not isinstance(raw_time_base, Mapping):
            raise ValueError("frame map source_time_base is missing")
        time_base = Fraction(
            _integer(raw_time_base.get("numerator"), "time base numerator"),
            _integer(raw_time_base.get("denominator"), "time base denominator"),
        )
        raw_frames = selected.get("frames")
        if not isinstance(raw_frames, list):
            raise ValueError("frame map frames are missing")
        frames: list[FrameMapEntry] = []
        for index, raw in enumerate(raw_frames):
            if not isinstance(raw, Mapping):
                raise ValueError("frame map entry must be an object")
            local = raw.get("output_frame_ordinal", index)
            source_ordinal = raw.get(
                "source_decoded_frame_ordinal", raw.get("ordinal")
            )
            source_pts = raw.get("source_pts", raw.get("pts"))
            frames.append(
                FrameMapEntry(
                    local_ordinal=_integer(local, "local frame ordinal"),
                    source_ordinal=_integer(source_ordinal, "source frame ordinal"),
                    source_pts=_integer(source_pts, "source PTS"),
                )
            )
        start = selected.get("source_start_pts")
        end = selected.get("source_end_pts_exclusive")
        return cls(
            clip_id=resolved_clip_id,
            source_start_pts=_integer(start, "source_start_pts"),
            source_end_pts_exclusive=_integer(end, "source_end_pts_exclusive"),
            time_base=time_base,
            frames=tuple(frames),
        )

    def entry_for_local_frame(self, frame: int) -> FrameMapEntry | None:
        if 0 <= frame < len(self.frames):
            candidate = self.frames[frame]
            if candidate.local_ordinal == frame:
                return candidate
        return None


@dataclass(frozen=True)
class SolveInterval:
    start_pts: int
    end_pts_exclusive: int
    core_start_pts: int
    core_end_pts_exclusive: int
    core_start_index: int
    core_end_index_exclusive: int
    frame_pts: tuple[int, ...]


@dataclass(frozen=True)
class SceneBridgeAnchor:
    source_pts: int
    source_frame: int
    target_frame: int
    camera: Mapping[str, float]


def derive_solve_interval(
    *,
    core_start_pts: int,
    core_end_pts_exclusive: int,
    source_frame_index: DecodedFrameIndex,
    overlap_seconds: Fraction = Fraction(4, 1),
) -> SolveInterval:
    if (
        type(core_start_pts) is not int
        or type(core_end_pts_exclusive) is not int
        or core_end_pts_exclusive <= core_start_pts
    ):
        raise SceneBridgeUnavailable("core interval must use increasing integer PTS")
    if overlap_seconds < 0:
        raise SceneBridgeUnavailable("solve overlap must not be negative")
    frames = source_frame_index.frames
    if not any(frame.pts == core_start_pts for frame in frames):
        raise SceneBridgeUnavailable("core start PTS is not a decoded source frame")
    if (
        core_start_pts < source_frame_index.source_start_pts
        or core_end_pts_exclusive > source_frame_index.source_end_pts_exclusive
    ):
        raise SceneBridgeUnavailable("core interval escapes the source video")
    overlap_pts = overlap_seconds / source_frame_index.time_base
    requested_start = Fraction(core_start_pts) - overlap_pts
    requested_end = Fraction(core_end_pts_exclusive) + overlap_pts
    selected_indices = [
        index
        for index, frame in enumerate(frames)
        if Fraction(frame.pts) >= requested_start and Fraction(frame.pts) < requested_end
    ]
    if not selected_indices:
        raise SceneBridgeUnavailable("solve interval contains no decoded source frames")
    first_index = selected_indices[0]
    last_index = selected_indices[-1]
    selected = frames[first_index : last_index + 1]
    end_pts = (
        frames[last_index + 1].pts
        if last_index + 1 < len(frames)
        else source_frame_index.source_end_pts_exclusive
    )
    core_indices = [
        index
        for index, frame in enumerate(selected)
        if core_start_pts <= frame.pts < core_end_pts_exclusive
    ]
    if not core_indices:
        raise SceneBridgeUnavailable("core interval contains no decoded source frames")
    return SolveInterval(
        start_pts=selected[0].pts,
        end_pts_exclusive=end_pts,
        core_start_pts=core_start_pts,
        core_end_pts_exclusive=core_end_pts_exclusive,
        core_start_index=core_indices[0],
        core_end_index_exclusive=core_indices[-1] + 1,
        frame_pts=tuple(frame.pts for frame in selected),
    )


def select_overlap_anchors(
    *,
    direction: str,
    source_core_map: FrameMap,
    source_camera_path: Mapping[int, Mapping[str, float]],
    target_solve_map: FrameMap,
    target_registered_frames: Collection[int],
    min_separation_seconds: Fraction = Fraction(1, 1),
) -> tuple[SceneBridgeAnchor, SceneBridgeAnchor]:
    if direction not in {"up", "down"}:
        raise ValueError("scene bridge direction must be up or down")
    if source_core_map.time_base != target_solve_map.time_base:
        raise SceneBridgeUnavailable("scene bridge frame maps use different time bases")
    if min_separation_seconds <= 0:
        raise ValueError("minimum anchor separation must be positive")
    source_by_pts: dict[int, tuple[int, Mapping[str, float]]] = {}
    for entry in source_core_map.frames:
        camera = source_camera_path.get(entry.local_ordinal)
        if camera is None:
            continue
        source_by_pts[entry.source_pts] = (
            entry.local_ordinal,
            _validated_camera(camera),
        )
    target_by_pts = {
        entry.source_pts: entry.local_ordinal
        for entry in target_solve_map.frames
        if entry.local_ordinal in target_registered_frames
    }
    common = sorted(set(source_by_pts) & set(target_by_pts))
    if len(common) < 2:
        raise SceneBridgeUnavailable(
            "scene overlap requires two common source PTS registered by target SfM"
        )
    first = common[0]
    last = next(
        (
            candidate
            for candidate in reversed(common[1:])
            if Fraction(candidate - first) * source_core_map.time_base
            >= min_separation_seconds
        ),
        None,
    )
    if last is None:
        raise SceneBridgeUnavailable("scene overlap anchors are too close")

    def build(source_pts: int) -> SceneBridgeAnchor:
        source_frame, camera = source_by_pts[source_pts]
        return SceneBridgeAnchor(
            source_pts=source_pts,
            source_frame=source_frame,
            target_frame=target_by_pts[source_pts],
            camera=dict(camera),
        )

    anchors = (build(first), build(last))
    if anchors[0].target_frame >= anchors[1].target_frame:
        raise SceneBridgeUnavailable("target overlap frames are not increasing")
    return anchors


def build_core_seed(
    *,
    core_map: FrameMap,
    core_registered_frames: Collection[int],
    solve_map: FrameMap,
    solve_camera_path: Mapping[int, Mapping[str, float]],
    fps: float,
    direction: str,
    source_clip_id: str,
    target_clip_id: str,
) -> dict[str, object]:
    if direction not in {"up", "down"}:
        raise ValueError("scene bridge direction must be up or down")
    if not math.isfinite(float(fps)) or float(fps) <= 0:
        raise ValueError("camera track fps must be finite and positive")
    if core_map.time_base != solve_map.time_base:
        raise SceneBridgeUnavailable("core and solve frame maps use different time bases")
    registered = sorted(set(core_registered_frames))
    if len(registered) < 2:
        raise SceneBridgeUnavailable("target core trajectory requires two registered frames")
    endpoint_frames = (registered[0], registered[-1])
    if endpoint_frames[0] == endpoint_frames[1]:
        raise SceneBridgeUnavailable("target core trajectory endpoints are identical")
    solve_frame_by_pts = {entry.source_pts: entry.local_ordinal for entry in solve_map.frames}
    keyframes: list[dict[str, object]] = []
    for core_frame in endpoint_frames:
        core_entry = core_map.entry_for_local_frame(core_frame)
        solve_frame = (
            None if core_entry is None else solve_frame_by_pts.get(core_entry.source_pts)
        )
        camera = None if solve_frame is None else solve_camera_path.get(solve_frame)
        if core_entry is None or solve_frame is None or camera is None:
            raise SceneBridgeUnavailable(
                "target core endpoint PTS is unavailable in the aligned solve path"
            )
        keyframes.append(
            {
                "frame": core_frame,
                "time": float(
                    Fraction(core_entry.source_pts - core_map.source_start_pts)
                    * core_map.time_base
                ),
                "source_pts": core_entry.source_pts,
                "source": "scene_overlap_anchor",
                "camera": dict(_validated_camera(camera)),
            }
        )
    return {
        "version": 1,
        "video": "",
        "fps": float(fps),
        "keyframes": keyframes,
        "scene_bridge": {
            "direction": direction,
            "source_clip_id": source_clip_id,
            "target_clip_id": target_clip_id,
            "timestamp_authority": "source_decoded_frame_integer_pts",
            "time_base": {
                "numerator": core_map.time_base.numerator,
                "denominator": core_map.time_base.denominator,
            },
        },
    }


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _validated_camera(value: Mapping[str, float]) -> dict[str, float]:
    fields = ("x", "y", "z", "yaw", "pitch", "roll", "fov")
    camera: dict[str, float] = {}
    for field in fields:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SceneBridgeUnavailable(f"source camera pose is missing {field}")
        number = float(raw)
        if not math.isfinite(number):
            raise SceneBridgeUnavailable(f"source camera pose has invalid {field}")
        camera[field] = number
    if not 1.0 < camera["fov"] < 179.0:
        raise SceneBridgeUnavailable("source camera pose has invalid fov")
    return camera
