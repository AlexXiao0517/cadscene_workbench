from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cadscene.alignment.keyframes import confirmed_keyframes
from cadscene.sfm.trajectory import load_sfm_trajectory


ALLOWED_KEYFRAME_INTERVALS = {120, 180, 240}
PLAN_FILENAME = "keyframe_plan.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def keyframe_plan_path(run_dir: str | Path) -> Path:
    return Path(run_dir) / "01_keyframes" / PLAN_FILENAME


def _manual_frames(camera_track: Mapping[str, Any]) -> set[int]:
    return {
        int(item["frame"])
        for item in confirmed_keyframes(dict(camera_track))
        if item.get("frame") is not None
    }


def _with_counts(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    frames = [dict(item) for item in payload.get("frames") or []]
    payload["frames"] = frames
    payload["completed_count"] = sum(item.get("status") == "completed" for item in frames)
    payload["pending_count"] = sum(item.get("status") != "completed" for item in frames)
    payload["updated_at"] = _now_iso()
    return payload


def create_keyframe_plan(
    trajectory_path: str | Path,
    camera_track: Mapping[str, Any],
    *,
    interval_frames: int,
) -> dict[str, Any]:
    """依据 SfM 原视频帧号生成待人工标定计划，不写入相机轨迹。"""
    interval = int(interval_frames)
    if interval not in ALLOWED_KEYFRAME_INTERVALS:
        raise ValueError("keyframe interval must be one of 120, 180, 240")
    manual_frames = _manual_frames(camera_track)
    if not manual_frames:
        raise ValueError("create a manual keyframe before generating a keyframe plan")
    trajectory = load_sfm_trajectory(trajectory_path)
    if len(trajectory.frames) == 0:
        raise ValueError("SfM trajectory contains no registered frames")

    start_frame = min(manual_frames)
    end_frame = int(trajectory.frames[-1])
    if start_frame > end_frame:
        raise ValueError("first manual keyframe is outside the SfM trajectory")
    frames = list(range(start_frame, end_frame + 1, interval))
    if frames[-1] != end_frame:
        frames.append(end_frame)
    plan = {
        "schema_version": "cadscene_keyframe_plan_v1",
        "interval_frames": interval,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "frames": [
            {
                "frame_index": frame,
                "status": "completed" if frame in manual_frames else "pending",
            }
            for frame in frames
        ],
        "created_at": _now_iso(),
    }
    return _with_counts(plan)


def sync_keyframe_plan(plan: Mapping[str, Any], camera_track: Mapping[str, Any]) -> dict[str, Any]:
    """根据现有人工关键帧刷新计划状态；计划帧本身不成为对齐锚点。"""
    manual_frames = _manual_frames(camera_track)
    payload = dict(plan)
    payload["frames"] = [
        {
            **dict(item),
            "status": "completed" if int(item["frame_index"]) in manual_frames else "pending",
        }
        for item in payload.get("frames") or []
    ]
    return _with_counts(payload)


def keyframe_plan_progress_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """只比较计划帧的完成状态，避免无变化保存破坏最终路线拟合的时序。"""
    previous = [(int(item["frame_index"]), str(item.get("status", "pending"))) for item in before.get("frames") or []]
    current = [(int(item["frame_index"]), str(item.get("status", "pending"))) for item in after.get("frames") or []]
    return previous != current


def load_keyframe_plan(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_keyframe_plan(path: str | Path, plan: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(plan), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def validate_quality_plan(plan_path: str | Path, aligned_camera_path: str | Path) -> None:
    """计划存在时，质量检测必须使用计划完成后的重新拟合结果。"""
    plan_file = Path(plan_path)
    if not plan_file.exists():
        raise ValueError("generate a keyframe plan before running quality detection")
    plan = load_keyframe_plan(plan_file)
    if int(plan.get("pending_count", 0)) > 0:
        raise ValueError("complete planned keyframes before running quality detection")
    aligned_path = Path(aligned_camera_path)
    if not aligned_path.exists() or aligned_path.stat().st_mtime_ns < plan_file.stat().st_mtime_ns:
        raise ValueError("rerun route fitting after completing the keyframe plan before running quality detection")
