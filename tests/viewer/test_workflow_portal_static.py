from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_portal_marks_video_and_cad_required_but_srt_optional() -> None:
    html = (ROOT / "apps/workflow_portal/index.html").read_text(encoding="utf-8")

    assert "视频" in html and "必传" in html
    assert "CAD" in html and "必传" in html
    assert "SRT" in html and "选填" in html


def test_portal_keeps_internal_identifiers_debug_only() -> None:
    html = (ROOT / "apps/workflow_portal/index.html").read_text(encoding="utf-8")
    script = (ROOT / "apps/workflow_portal/workflow_portal.js").read_text(encoding="utf-8")
    css = (ROOT / "apps/workflow_portal/style.css").read_text(encoding="utf-8")

    assert 'id="portalDebug"' in html
    assert "debug=1" in script
    assert ".debug-only" in css


def test_portal_uses_existing_upload_apis_and_viewer_route() -> None:
    script = (ROOT / "apps/workflow_portal/workflow_portal.js").read_text(encoding="utf-8")

    for route in (
        "/api/workflow/create-dataset",
        "/api/workflow/upload-video",
        "/api/workflow/upload-cad",
        "/api/workflow/upload-srt",
        "/apps/web_camera_viewer/",
    ):
        assert route in script


def test_existing_viewer_has_manifest_backed_interface_only_copy() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "interface_only" in script
    assert "trajectoryWorkflowLoaded" in script
    assert "正在确认轨迹工作模式" in script
