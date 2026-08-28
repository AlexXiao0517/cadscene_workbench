from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from cadscene.projects.scene_bridges import FrameMap


@dataclass(frozen=True)
class TrajectoryPartitionResult:
    solve_path: Path
    core_path: Path
    solve_pose_count: int
    core_pose_count: int
    core_registered_pose_count: int


def partition_sfm_trajectory(
    raw_path: Path,
    solve_frame_map_path: Path,
    core_frame_map_path: Path,
    *,
    solve_output_path: Path,
    core_output_path: Path,
) -> TrajectoryPartitionResult:
    """按精确 source PTS 将一次 solve 重建投影为兼容的核心轨迹。"""

    raw = _load_mapping(raw_path, "SfM trajectory")
    solve_map = FrameMap.from_dict(
        _load_mapping(solve_frame_map_path, "solve frame map")
    )
    core_map = FrameMap.from_dict(
        _load_mapping(core_frame_map_path, "core frame map")
    )
    if solve_map.time_base != core_map.time_base:
        raise ValueError("solve and core frame maps use different time bases")
    poses = raw.get("poses")
    if not isinstance(poses, list) or not poses:
        raise ValueError("SfM trajectory contains no poses")

    solve_pts_by_frame = {
        entry.local_ordinal: entry.source_pts for entry in solve_map.frames
    }
    core_frame_by_pts = {
        entry.source_pts: entry.local_ordinal for entry in core_map.frames
    }
    solve_poses: list[dict[str, object]] = []
    core_poses: list[dict[str, object]] = []
    seen_frames: set[int] = set()
    for raw_pose in poses:
        if not isinstance(raw_pose, Mapping):
            raise ValueError("SfM trajectory pose must be an object")
        frame = raw_pose.get("frame_index")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise ValueError("SfM trajectory frame_index must be a non-negative integer")
        if frame in seen_frames:
            raise ValueError("SfM trajectory frame_index must be unique")
        seen_frames.add(frame)
        source_pts = solve_pts_by_frame.get(frame)
        if source_pts is None:
            raise ValueError(
                f"SfM trajectory frame {frame} is outside solve frame map"
            )
        solve_pose = {**dict(raw_pose), "source_pts": source_pts}
        solve_poses.append(solve_pose)
        core_frame = core_frame_by_pts.get(source_pts)
        if core_frame is not None:
            core_poses.append({**solve_pose, "frame_index": core_frame})

    solve_poses.sort(key=lambda pose: int(pose["frame_index"]))
    core_poses.sort(key=lambda pose: int(pose["frame_index"]))
    registered_core = sum(pose.get("registered") is True for pose in core_poses)
    if registered_core < 2:
        raise ValueError("SfM trajectory must contain at least two registered core poses")

    authority = {
        "timestamp_authority": "source_decoded_frame_integer_pts",
        "source_time_base": {
            "numerator": solve_map.time_base.numerator,
            "denominator": solve_map.time_base.denominator,
        },
    }
    solve_payload = {**dict(raw), **authority, "poses": solve_poses}
    core_payload = {**dict(raw), **authority, "poses": core_poses}
    _write_pair_atomically(
        solve_output_path,
        solve_payload,
        core_output_path,
        core_payload,
    )
    return TrajectoryPartitionResult(
        solve_path=solve_output_path,
        core_path=core_output_path,
        solve_pose_count=len(solve_poses),
        core_pose_count=len(core_poses),
        core_registered_pose_count=registered_core,
    )


def _load_mapping(path: Path, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _write_pair_atomically(
    first_path: Path,
    first_payload: Mapping[str, object],
    second_path: Path,
    second_payload: Mapping[str, object],
) -> None:
    if first_path.resolve(strict=False) == second_path.resolve(strict=False):
        raise ValueError("solve and core trajectory outputs must be different files")
    temporary: list[tuple[Path, Path]] = []
    try:
        for path, payload in (
            (first_path, first_payload),
            (second_path, second_payload),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            candidate = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.append((candidate, path))
        for candidate, path in temporary:
            os.replace(candidate, path)
    finally:
        for candidate, _path in temporary:
            candidate.unlink(missing_ok=True)
