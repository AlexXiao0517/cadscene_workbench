from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


WORKFLOW_STAGES = ("upload", "sfm", "keyframes", "quality", "render")
VALID_STATUSES = {"pending", "running", "success", "failed", "cancelled"}
MANIFEST_STAGE_MAP = {
    "sfm": "sfm",
    "alignment": "quality",
    "quality": "quality",
    "viewer_scene": "quality",
    "road_surface": "quality",
    "render": "render",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _stage_state() -> dict[str, Any]:
    return {"status": "pending", "progress": 0.0, "message": ""}


def create_job_status(run_id: str) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "current_stage": "upload",
        "status": "pending",
        "progress": 0.0,
        "message": "",
        "stages": {name: _stage_state() for name in WORKFLOW_STAGES},
        "updated_at": now_iso(),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.005 * (2**attempt), 0.05))
    finally:
        temporary.unlink(missing_ok=True)


class JobStatusStore:
    def __init__(self, path: str | Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            payload = create_job_status(self.run_id)
            _atomic_write_json(self.path, payload)
            return payload
        return json.loads(self.path.read_text(encoding="utf-8-sig"))

    def update_stage(
        self,
        stage: str,
        *,
        status: str,
        progress: float,
        message: str,
        error: str | None = None,
        log_file: str | Path | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        if stage not in WORKFLOW_STAGES:
            raise ValueError(f"unknown workflow stage: {stage}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid workflow status: {status}")
        payload = self.load()
        stage_payload = payload["stages"][stage]
        stage_payload.update(
            {
                "status": status,
                "progress": max(0.0, min(1.0, float(progress))),
                "message": str(message),
                "updated_at": now_iso(),
            }
        )
        if error:
            stage_payload["error"] = str(error)
        else:
            stage_payload.pop("error", None)
        if log_file is not None:
            stage_payload["log_file"] = str(log_file)
        if operation is not None:
            stage_payload["operation"] = str(operation)
            payload["operation"] = str(operation)
        payload["current_stage"] = stage
        payload["status"] = status
        payload["message"] = str(message)
        if error:
            payload["error"] = str(error)
        else:
            payload.pop("error", None)
        if log_file is not None:
            payload["log_file"] = str(log_file)
        payload["progress"] = sum(float(item["progress"]) for item in payload["stages"].values()) / len(WORKFLOW_STAGES)
        payload["updated_at"] = now_iso()
        _atomic_write_json(self.path, payload)
        return payload

    def sync_from_manifest(self, manifest_path: str | Path) -> dict[str, Any]:
        payload = self.load()
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8-sig"))
        for record in manifest.get("stages", []):
            workflow_stage = MANIFEST_STAGE_MAP.get(str(record.get("stage_name")))
            if not workflow_stage:
                continue
            record_status = str(record.get("status", "pending"))
            if record_status == "success":
                payload["stages"][workflow_stage].update(
                    {"status": "success", "progress": 1.0, "message": f"{record.get('stage_name')} 已完成"}
                )
            elif record_status == "failed":
                payload["stages"][workflow_stage].update(
                    {"status": "failed", "message": f"{record.get('stage_name')} 执行失败"}
                )
        payload["progress"] = sum(float(item["progress"]) for item in payload["stages"].values()) / len(WORKFLOW_STAGES)
        payload["updated_at"] = now_iso()
        _atomic_write_json(self.path, payload)
        return payload


def append_ignored_suggestion(
    path: str | Path,
    *,
    frame_index: int,
    reason: str = "用户确认无需补帧",
) -> dict[str, Any]:
    target = Path(path)
    payload: dict[str, Any] = {"ignored": []}
    if target.exists():
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    ignored = list(payload.get("ignored") or [])
    ignored = [item for item in ignored if int(item.get("frame_index", -1)) != int(frame_index)]
    ignored.append(
        {
            "frame_index": int(frame_index),
            "reason": str(reason),
            "created_at": now_iso(),
        }
    )
    payload["ignored"] = sorted(ignored, key=lambda item: int(item["frame_index"]))
    _atomic_write_json(target, payload)
    return payload
