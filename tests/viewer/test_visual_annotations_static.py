from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "apps" / "web_camera_viewer"


def test_render_stage_contains_visual_label_toolbar_editor_and_overlay() -> None:
    html = (VIEWER / "index.html").read_text(encoding="utf-8")

    assert 'id="annotationToolbar"' in html
    assert 'data-annotation-mode="cad_anchor"' in html
    assert 'data-annotation-mode="video_track"' in html
    assert 'id="annotationText"' in html
    assert 'id="annotationFontSize"' in html
    assert 'id="annotationStartPts"' in html
    assert 'id="annotationEndPts"' in html
    assert 'id="annotationVisible"' in html
    assert 'id="annotationDelete"' in html
    assert 'id="annotationReanchor"' in html
    assert 'id="annotationTrackingState"' in html
    assert 'id="annotationOverlay"' in html
    assert '<script src="./annotations.js?' in html


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


def test_annotation_overlay_is_dom_based_and_does_not_add_per_frame_three_meshes() -> None:
    script = (VIEWER / "annotations.js").read_text(encoding="utf-8")
    css = (VIEWER / "style.css").read_text(encoding="utf-8")

    assert "requestAnimationFrame" in script
    assert "replaceChildren" not in script
    assert "THREE.Sprite" not in script
    assert ".annotation-label" in css
    assert "pointer-events: none" in css
