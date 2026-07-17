from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_workflow_routing_documentation_describes_all_three_modes() -> None:
    text = (ROOT / "docs/workflow_routing.md").read_text(encoding="utf-8")

    assert all(mode in text for mode in ("sfm_only", "srt_sfm_fused", "srt_full_pose"))


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


def test_portal_shows_selected_files_before_uploading() -> None:
    script = (ROOT / "apps/workflow_portal/workflow_portal.js").read_text(encoding="utf-8")

    assert 'addEventListener("change", () => {' in script
    assert '"已选择，等待上传"' in script


def test_existing_viewer_has_manifest_backed_interface_only_copy() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "interface_only" in script
    assert "trajectoryWorkflowLoaded" in script
    assert "正在确认轨迹工作模式" in script


def test_viewer_manifest_fetch_failure_falls_back_to_ready_sfm_only() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert 'trajectory_mode: "sfm_only"' in script
    assert 'implementation_status: "ready"' in script
    assert "trajectoryWorkflowLoaded = true" in script
