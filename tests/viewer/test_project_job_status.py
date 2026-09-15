from __future__ import annotations

import json
import subprocess
from pathlib import Path


WORKFLOW = Path("apps/web_camera_viewer/workflow.js")


def _owns_project_status(*, trajectory: str | None = None, render: str | None = None) -> bool:
    source = WORKFLOW.read_text(encoding="utf-8")
    start = source.index("function projectWorkbenchTrajectoryOwnsStatus()")
    end = source.index("async function refreshProjectWorkbenchSession", start)
    function_source = source[start:end]
    context = {
        "projectWorkbenchToken": "workbench-token",
        "projectWorkbenchTrajectoryStatus": trajectory,
        "projectWorkbenchRenderStatus": render,
    }
    script = (
        "const vm=require('node:vm');"
        f"const context={json.dumps(context)};"
        f"const source={json.dumps(function_source)};"
        "process.stdout.write(JSON.stringify(vm.runInNewContext("
        "`${source}; projectWorkbenchTrajectoryOwnsStatus()`, context)));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_only_active_project_jobs_own_workflow_status_polling() -> None:
    for status in ("queued", "preparing", "running", "validating", "cancel_requested"):
        assert _owns_project_status(render=status)
        assert _owns_project_status(trajectory=status)

    for status in ("success", "failed", "interrupted", "cancelled", "stale_input", "superseded"):
        assert not _owns_project_status(render=status)
        assert not _owns_project_status(trajectory=status)
