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


def test_new_project_flow_explicitly_activates_a_completed_candidate_analysis() -> None:
    api, script, html = (
        _read("portal_api.js"),
        _read("workflow_portal.js"),
        _read("index.html"),
    )

    assert "activateCandidateAnalysis" in api
    assert "/analysis/activate" in api
    assert 'project_state === "analysis_candidate_ready"' in script
    assert "snapshot.candidate_analysis_revision" in script
    assert "snapshot.component_revisions.project" in script
    assert "snapshot.component_revisions.clips" in script
    assert "await activateCandidateAnalysis(" in script
    assert "workflow_portal.js?v=20260807-upload-v9" in html


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

    assert 'pane.classList.add("has-file")' in script
    assert ".drop-copy[hidden]" in css
    assert ".upload-glyph[hidden]" in css
    assert ".file-preview[hidden]" in css


def test_portal_keeps_the_mockup_information_density() -> None:
    html = _read("index.html")

    assert 'class="srt-row"' in html
    assert '<details class="optional-upload">' not in html
    assert "文件已上传，可以新建项目。解析进度将在弹窗中显示。" in html


def test_portal_uses_new_project_copy_everywhere() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")

    assert "创建视频叠加任务" not in html
    assert "创建叠加任务" not in html
    assert "创建叠加任务" not in script
    assert "新建项目" in html


def test_upload_columns_are_not_wrapped_in_visual_cards() -> None:
    html, css = _read("index.html"), _read("style.css")

    assert 'class="upload-pane"' in html
    assert 'class="upload-card"' not in html
    assert ".upload-pane {" in css
    assert ".upload-pane { position: relative; min-width: 0; padding: 0; border: 0; background: transparent; }" in css


def test_portal_uses_transparent_cad_video_logo_and_pill_buttons() -> None:
    html, css = _read("index.html"), _read("style.css")
    logo = ROOT / "apps/workflow_portal/assets/mediaflow-cad-video-logo.svg"

    assert logo.is_file()
    assert "mediaflow-cad-video-logo.svg" in html
    assert "<svg" in logo.read_text(encoding="utf-8")
    assert "<rect" not in logo.read_text(encoding="utf-8")
    assert "border-radius: 999px" in css


def test_portal_supports_persisted_light_and_dark_themes() -> None:
    html, script, css = _read("index.html"), _read("workflow_portal.js"), _read("style.css")

    assert 'id="themeToggle"' in html
    assert 'data-theme="light"' in css
    assert "localStorage.getItem(THEME_STORAGE_KEY)" in script
    assert "localStorage.setItem(THEME_STORAGE_KEY" in script
    assert 'setAttribute("data-theme", theme)' in script


def test_theme_toggle_is_a_flat_icon_only_control_with_hover_help() -> None:
    html, script, css = _read("index.html"), _read("workflow_portal.js"), _read("style.css")
    toggle = html[html.index('<button id="themeToggle"') : html.index("</button>", html.index('<button id="themeToggle"'))]
    theme_css = css[css.index(".theme-toggle {") : css.index(".theme-toggle:hover")]

    assert "<span>" not in toggle
    assert 'title="切换为浅色模式"' in toggle
    assert 'setAttribute("title"' in script
    assert "width: 36px" in theme_css and "height: 36px" in theme_css
    assert "padding: 0" in theme_css
    assert "border: 0" in theme_css
    assert "background: transparent" in theme_css
    assert "border-radius" not in theme_css


def test_upload_prompts_use_distinct_transparent_gray_png_icons() -> None:
    html, css = _read("index.html"), _read("style.css")
    assets = ROOT / "apps/workflow_portal/assets"
    names = ("upload-video-gray.png", "upload-cad-gray.png")

    for name in names:
        data = (assets / name).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[24] == 8  # 8-bit channels
        assert data[25] == 6  # RGBA, including transparent background
        assert f'/apps/workflow_portal/assets/{name}' in html
    glyph_css = css[css.index(".upload-glyph {") : css.index(".drop-copy {")]
    assert ".upload-glyph img" in glyph_css
    assert "opacity:" in glyph_css
    assert "background: transparent" in glyph_css
    assert ".upload-glyph svg" not in glyph_css


def test_new_project_title_lives_in_the_flat_top_navigation() -> None:
    html, css = _read("index.html"), _read("style.css")

    assert 'class="top-navigation"' in html
    assert 'class="current-product">新建项目</span>' in html
    assert 'class="heading-copy"' not in html
    assert ".current-product {" in css


def test_upload_pane_has_distinct_uploading_and_completed_visual_states() -> None:
    script, css = _read("workflow_portal.js"), _read("style.css")

    assert 'pane.classList.add("is-uploading")' in script
    assert 'pane.classList.remove("is-uploading")' in script
    assert 'progressWrap.hidden = true' in script
    assert ".upload-pane.is-uploading .drop-zone" in css
    assert ".upload-pane.is-complete .drop-zone" in css


def test_successful_upload_hides_progress_instead_of_leaving_one_hundred_percent_visible() -> None:
    script = _read("workflow_portal.js")

    success_block = script[script.index("state.completed[kind] = true") : script.index("return result;")]
    assert 'progressWrap.hidden = true' in success_block


def test_flat_navigation_has_no_rule_and_uses_hover_color_only() -> None:
    css = _read("style.css")

    navigation = css[css.index(".top-navigation {") : css.index(".brand {")]
    product = css[css.index(".current-product {") : css.index(".theme-toggle {")]
    assert "border-bottom" not in navigation
    assert "border-bottom" not in product
    assert ".current-product:hover" in css


def test_flat_portal_uses_no_box_shadows() -> None:
    css = _read("style.css")

    assert "box-shadow:" not in css


def test_completed_video_and_cad_previews_fill_the_drop_zone_without_file_copy() -> None:
    html, script, css = _read("index.html"), _read("workflow_portal.js"), _read("style.css")

    assert '<video muted preload="metadata"></video>' in html
    assert 'id="cadPreviewImage"' in html
    assert 'class="file-name"' not in html
    assert 'class="file-meta"' not in html
    assert 'thumbnails/cad' in script
    assert "object-fit: cover" in css
    assert ".file-preview { position: absolute; inset: 2px;" in css


def test_reselected_cad_preview_uses_the_published_asset_fingerprint() -> None:
    html, script = _read("index.html"), _read("workflow_portal.js")
    cad_success = script[
        script.index('if (kind === "cad")') : script.index(
            '$(`[data-reselect="${kind}"]`).hidden = false'
        )
    ]

    assert "result.fingerprint" in cad_success
    assert "?asset=${encodeURIComponent(result.fingerprint)}" in cad_success
    assert "workflow_portal.js?v=20260807-upload-v9" in html


def test_preview_keeps_the_drop_zone_as_its_containing_block_across_upload_states() -> None:
    css = _read("style.css")

    drop_zone = css[css.index(".drop-zone {") : css.index(".drop-zone:hover")]
    preview = css[css.index(".file-preview {") : css.index(".file-preview video")]
    assert "position: relative" in drop_zone
    assert "inset: 2px" in preview
    assert "border-radius: 14px" in preview
    assert "overflow: hidden" in preview


def test_analysis_modal_has_close_control_and_animated_circular_real_progress() -> None:
    html, script, css = _read("index.html"), _read("workflow_portal.js"), _read("style.css")

    assert 'id="analysisClose"' in html
    assert 'id="taskCircleProgress"' in html
    assert 'id="taskCirclePercent"' in html
    assert "strokeDashoffset" in script
    assert "state.overlayDismissed" in script
    assert ".progress-orbit" in css and "@keyframes orbit" in css


def test_upload_layout_uses_one_card_layer_only() -> None:
    css = _read("style.css")

    assert "form { padding: 0; border: 0; background: transparent; }" in css


def test_portal_busts_cached_assets_for_the_stable_preview_layout() -> None:
    html = _read("index.html")

    assert "20260807-upload-v9" in html


def test_existing_viewer_has_manifest_backed_interface_only_copy() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert "interface_only" in script
    assert "trajectoryWorkflowLoaded" in script


def test_viewer_manifest_fetch_failure_falls_back_to_ready_sfm_only() -> None:
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert 'trajectory_mode: "sfm_only"' in script
    assert 'implementation_status: "ready"' in script
    assert "trajectoryWorkflowLoaded = true" in script
