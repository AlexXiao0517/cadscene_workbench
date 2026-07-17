from __future__ import annotations

from pathlib import Path


APP = Path("apps/web_camera_viewer")


def test_sfm_panel_shows_runtime_summary_and_debug_only_controls() -> None:
    html = (APP / "index.html").read_text(encoding="utf-8")
    script = (
        (APP / "workflow.js").read_text(encoding="utf-8")
        + (APP / "sfm_backend_ui.js").read_text(encoding="utf-8")
    )

    for control_id in (
        "workflowSfmBackend",
        "workflowSfmDevice",
        "workflowSfmSubstage",
        "workflowSfmElapsed",
    ):
        assert control_id in html
    for control_id in (
        "workflowSfmBackendSelect",
        "workflowSfmDeviceSelect",
        "workflowSfmGpuIndex",
        "workflowSfmNoCpuFallback",
    ):
        assert control_id in html
    assert "dev-only-control" in html
    assert "debugEnabled" in script
    assert "gpu_index" in script
    assert "no_cpu_fallback" in script
    assert "/api/workflow/run-stage" in script
    assert '<option value="pycolmap" selected>pycolmap</option>' in html
    assert '<option value="cpu" selected>CPU</option>' in html
    assert '|| "pycolmap"' in script
    assert '|| "cpu"' in script
