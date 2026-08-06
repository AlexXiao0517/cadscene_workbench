from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / "apps/workflow_portal" / name).read_text(encoding="utf-8")


def test_workflow_routing_documentation_describes_all_three_modes() -> None:
    text = (ROOT / "docs/workflow_routing.md").read_text(encoding="utf-8")
    assert all(mode in text for mode in ("sfm_only", "srt_sfm_fused", "srt_full_pose"))


def test_portal_marks_video_and_cad_required_but_srt_optional() -> None:
    html = _read("index.html")
    assert "上传视频" in html and "必传" in html
    assert "上传 CAD 图纸" in html and "必传" in html
    assert "SRT" in html and "可选" in html


def test_portal_keeps_internal_identifiers_debug_only() -> None:
    html, script, css = _read("index.html"), _read("workflow_portal.js"), _read("style.css")
    assert 'id="portalDebug"' in html
    assert "debug=1" in script
    assert ".debug-only" in css


def test_portal_keeps_legacy_routes_documented_and_uses_project_workspace() -> None:
    script = _read("workflow_portal.js")
    for route in (
        "/api/workflow/create-dataset",
        "/api/workflow/upload-video",
        "/api/workflow/upload-cad",
        "/api/workflow/upload-srt",
        "/apps/project_workspace/",
    ):
        assert route in script


def test_portal_uploads_each_selected_file_immediately() -> None:
    script = _read("workflow_portal.js")
    assert "startAssetUpload(kind, file)" in script
    assert "state.uploads.video" in script and "state.uploads.cad" in script


def test_portal_removes_the_legacy_hovering_rotation_choice() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")
    assert 'id="portalMotionMode"' not in html
    assert 'id="portalHoveringDeclared"' not in html
    assert "无人机悬停" not in html
    assert "updateMotionModeAvailability" not in script


def test_portal_waits_for_project_analysis() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")
    assert "hoveringDeclared" not in script
    assert "waitForAnalysisCompletion" in script
    assert 'id="analysisOverlay"' in html


def test_portal_uses_real_upload_bytes_and_never_timer_drives_upload_progress() -> None:
    api, script = _read("portal_api.js"), _read("workflow_portal.js")
    assert "event.loaded" in api and "event.total" in api
    assert "useCurrentRevision=1" in api
    assert "setInterval" not in api
    assert "simulateProgress" not in script
    assert "fakeProgress" not in script


def test_portal_has_two_drag_drop_cards_and_four_real_task_stages() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")
    assert 'data-upload-kind="video"' in html
    assert 'data-upload-kind="cad"' in html
    assert 'accept=".mp4,video/mp4"' in html
    assert 'accept=".dxf,application/dxf"' in html
    assert all(
        f'id="taskStage{stage}"' in html
        for stage in ("Upload", "Cad", "Video", "Workspace")
    )
    assert 'id="taskOverallProgress"' in html
    assert 'addEventListener("dragover"' in script
    assert 'addEventListener("drop"' in script


def test_portal_shows_video_preview_and_only_enables_create_after_both_uploads() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")
    assert 'id="videoPreview"' in html
    assert "URL.createObjectURL(file)" in script
    assert "state.completed.video && state.completed.cad" in script
    assert 'id="portalSubmit"' in html and "disabled" in html


def test_completed_upload_replaces_the_picker_copy_instead_of_stacking_both() -> None:
    script, css = _read("workflow_portal.js"), _read("style.css")

    assert 'card.classList.add("has-file")' in script
    assert ".drop-copy[hidden]" in css
    assert ".upload-glyph[hidden]" in css
    assert ".file-preview[hidden]" in css


def test_portal_keeps_the_mockup_information_density() -> None:
    html = _read("index.html")

    assert 'class="srt-row"' in html
    assert '<details class="optional-upload">' not in html
    assert "文件已上传，可以创建叠加任务。解析进度将在弹窗中显示。" in html


def test_existing_viewer_has_manifest_backed_interface_only_copy() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert "interface_only" in script
    assert "trajectoryWorkflowLoaded" in script


def test_viewer_manifest_fetch_failure_falls_back_to_ready_sfm_only() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert 'trajectory_mode: "sfm_only"' in script
    assert 'implementation_status: "ready"' in script
    assert "trajectoryWorkflowLoaded = true" in script
