from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "apps" / "web_camera_viewer"


def test_render_stage_contains_visual_label_toolbar_editor_and_overlay() -> None:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")

    assert 'id="annotationToolbar"' in html
    assert 'data-annotation-mode="cad_anchor"' in html
    assert 'data-annotation-mode="video_track"' in html
    assert 'id="annotationTitle"' in html
    assert 'id="annotationBody"' in html
    assert 'id="annotationPanelWidth"' in html
    assert 'id="annotationBackgroundOpacity"' in html
    assert 'id="annotationFontSize"' in html
    assert 'id="annotationStartPts"' in html
    assert 'id="annotationEndPts"' in html
    assert 'id="annotationVisible"' in html
    assert 'id="annotationDelete"' in html
    assert 'id="annotationReanchor"' in html
    assert 'id="annotationTrackingState"' in html
    assert 'id="annotationOverlay"' in html
    assert '<script src="./annotations.js?' in html
    assert '<script src="./callout_layout.js?' in html


def test_annotation_ui_uses_revisioned_api_drag_offsets_and_raycast() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")
    legacy = (VIEWER / "viewer_legacy.js").read_text(encoding="utf-8")

    assert "expected_revision" in script
    assert "expected_annotation_revision" in script
    assert 'method: "DELETE"' in script
    assert "screen_offset" in script
    assert "pointermove" in script
    assert "/track" in script
    assert "cadscenePickCadWorld" in script
    assert "displayToSource" in script
    assert "currentTime *" not in script
    assert "cadscenePickCadWorld" in legacy
    assert "intersectObjects" in legacy


def test_annotation_overlay_is_dom_based_and_does_not_add_per_frame_three_meshes() -> (
    None
):
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")
    css = (VIEWER / "style.css").read_text(encoding="utf-8")

    assert "requestAnimationFrame" in script
    assert "replaceChildren" not in script
    assert "THREE.Sprite" not in script
    assert ".annotation-label" in css
    assert ".engineering-callout" in css
    assert ".engineering-callout-title" in css
    assert ".engineering-callout-body" in css
    assert ".engineering-callout-leader" in css
    assert "pointer-events: none" in css


def test_engineering_callout_uses_shared_layout_and_structured_content() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")

    assert "CadsceneCalloutLayout.layoutCallout" in script
    assert "annotation.content?.title" in script
    assert "annotation.content?.body" in script
    assert 'createElementNS("http://www.w3.org/2000/svg", "polyline")' in script
    assert "panel_rect" in script
    assert "leader_points" in script
    assert "entry.label.textContent = annotation.text" not in script
    assert "visual.anchor_source_xy" in script
    assert "sourceToDisplayPoint" in script
    assert "viewport_size: [video.videoWidth, video.videoHeight]" in script
    assert "viewport_size: [overlay.clientWidth, overlay.clientHeight]" not in script


def test_render_stage_uses_full_label_editor_and_collapses_camera_settings() -> None:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    workflow = (VIEWER / "workflow.js").read_text(encoding="utf-8")

    assert 'id="annotationPanel"' in html
    assert 'id="cameraSettingsDetails"' in html
    assert 'id="annotationEditorPanel"' in html
    assert "在右侧 CAD 添加" in html
    assert "在左侧视频添加" in html
    assert 'document.querySelector("#annotationPanel")' in workflow
    assert 'document.querySelector("#cameraSettingsDetails")' in workflow
    assert 'annotationPanel.hidden = selectedWorkflowStage !== "render"' in workflow
    assert 'cameraSettings.open = selectedWorkflowStage !== "render"' in workflow


def test_new_label_is_selected_for_immediate_text_editing() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")

    assert "function focusSelectedAnnotationEditor" in script
    assert 'editor.title?.addEventListener("keydown"' in script
    assert 'editor.body?.addEventListener("keydown"' in script
    assert "await saveSelectedAnnotation()" in script
    assert script.count("focusSelectedAnnotationEditor();") >= 2
    video_create = script[script.index('createAnnotation("video_track"') :]
    video_create = video_create[: video_create.index("function beginLabelDrag")]
    assert "state.pendingInitialTrackingId = annotation.annotation_id;" in video_create
    assert "await runTracking(annotation);" not in video_create
    save = script[script.index("async function saveSelectedAnnotation()") :]
    assert "state.pendingInitialTrackingId === updated.annotation_id" in save
    assert "await runTracking(updated);" in save
    assert save.index("await runTracking(updated);") < save.index(
        "state.pendingInitialTrackingId = null;"
    )


def test_video_target_supports_click_roi_and_visible_drag_selection() -> None:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")

    assert 'id="annotationRoiSelection"' in html
    assert "DEFAULT_VIDEO_ROI_SIZE" in script
    assert 'videoLayer.addEventListener("pointermove"' in script
    assert "updateRoiSelection" in script


def test_video_target_selection_suppresses_native_video_click_playback() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")

    assert (
        'video.addEventListener("click", suppressVideoPlaybackWhileSelecting, true)'
        in script
    )
    assert "suppressNextVideoClick: false" in script
    assert "state.suppressNextVideoClick = true;" in script
    suppress = script[
        script.index("function suppressVideoPlaybackWhileSelecting") : script.index(
            'video.addEventListener("click"',
            script.index("function suppressVideoPlaybackWhileSelecting"),
        )
    ]
    assert "event.preventDefault()" in suppress
    assert "event.stopImmediatePropagation()" in suppress
    assert "state.suppressNextVideoClick = false;" in suppress


def test_cad_anchor_creation_has_inspect_view_feedback_and_projection_guard() -> None:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")
    legacy = (VIEWER / "viewer_legacy.js").read_text(encoding="utf-8")
    css = (VIEWER / "style.css").read_text(encoding="utf-8")

    assert 'id="cadAnnotationOverlay"' in html
    assert "cadsceneProjectCadWorldToInspect" in legacy
    assert "cadsceneProjectCadWorldToInspect" in script
    assert "cadAnchorCreationDecision" in script
    assert script.index("cadAnchorCreationDecision") < script.index(
        'createAnnotation("cad_anchor"'
    )
    assert "#cadAnnotationOverlay" in css
    assert "cadNodes" in script
    cad_click = script[
        script.index('sceneContainer.addEventListener("click"') : script.index(
            'videoLayer.addEventListener("pointerdown"'
        )
    ]
    assert 'event.target.closest?.(".annotation-label")' in cad_click
    decision_branch = cad_click[
        cad_click.index("if (!decision.ok)") : cad_click.index(
            'await createAnnotation("cad_anchor"'
        )
    ]
    assert "return;" not in decision_branch
    assert "右侧 CAD 中仍可编辑" in cad_click


def test_selected_hidden_cad_anchor_reports_projection_reason() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")

    hidden_branch = script[
        script.index("const visible = Boolean") : script.index(
            "const anchor = visual.anchor_source_xy"
        )
    ]
    assert "CAD 投影已隐藏" in hidden_branch
    assert "visual?.reason" in hidden_branch
