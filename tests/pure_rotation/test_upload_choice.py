from pathlib import Path


def test_upload_choice_defaults_to_sfm_and_sends_explicit_hovering_field() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert 'id="workflowHoveringDeclared"' in html
    assert "hoveringDeclared" in script
    assert "checked" not in html.split('id="workflowHoveringDeclared"', 1)[1].split(">", 1)[0]
