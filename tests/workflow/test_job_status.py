from __future__ import annotations

import json
from pathlib import Path

from cadscene.workflow.job_status import JobStatusStore, create_job_status


def test_job_status_schema_contains_all_workflow_stages() -> None:
    status = create_job_status("demo")

    assert status["run_id"] == "demo"
    assert status["status"] == "pending"
    assert status["progress"] == 0.0
    assert set(status["stages"]) == {"upload", "sfm", "keyframes", "quality", "render"}
    for stage in status["stages"].values():
        assert set(stage) >= {"status", "progress", "message"}


def test_store_writes_and_updates_stage_status(tmp_path: Path) -> None:
    path = tmp_path / "job_status.json"
    store = JobStatusStore(path, run_id="r1")

    updated = store.update_stage("sfm", status="running", progress=0.4, message="正在匹配特征")

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert updated["current_stage"] == "sfm"
    assert persisted["stages"]["sfm"]["status"] == "running"
    assert persisted["stages"]["sfm"]["progress"] == 0.4
    assert persisted["message"] == "正在匹配特征"


def test_failed_stage_records_error(tmp_path: Path) -> None:
    store = JobStatusStore(tmp_path / "job_status.json", run_id="r1")

    status = store.update_stage(
        "quality",
        status="failed",
        progress=0.5,
        message="质量检测失败",
        error="alignment.json missing",
    )

    assert status["status"] == "failed"
    assert status["error"] == "alignment.json missing"
    assert status["stages"]["quality"]["error"] == "alignment.json missing"


def test_store_can_sync_successful_stages_from_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "stages": [
                    {"stage_name": "sfm", "status": "success"},
                    {"stage_name": "alignment", "status": "success"},
                    {"stage_name": "quality", "status": "success"},
                    {"stage_name": "render", "status": "success"},
                ]
            }
        ),
        encoding="utf-8",
    )
    store = JobStatusStore(tmp_path / "job_status.json", run_id="r1")

    status = store.sync_from_manifest(manifest)

    assert status["stages"]["sfm"]["status"] == "success"
    assert status["stages"]["quality"]["status"] == "success"
    assert status["stages"]["render"]["status"] == "success"


def test_job_status_keeps_operation_name_when_workflow_stage_is_keyframes(tmp_path: Path) -> None:
    store = JobStatusStore(tmp_path / "job_status.json", run_id="demo")

    payload = store.update_stage(
        "keyframes",
        status="running",
        progress=0.05,
        message="route fitting started",
        operation="alignment",
    )

    assert payload["current_stage"] == "keyframes"
    assert payload["operation"] == "alignment"
    assert payload["stages"]["keyframes"]["operation"] == "alignment"
