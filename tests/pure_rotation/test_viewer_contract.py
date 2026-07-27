from pathlib import Path


def test_viewer_has_isolated_pure_rotation_controls_and_no_sfm_path_contract() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    for identifier in ("pureRotationPanel", "pureRotationTrack", "pureRotationPlacement", "pureRotationCorrections"):
        assert identifier in html
    assert "/api/pure-rotation/trajectory" in script
    assert "pts_time_sec" in script
    assert "pure_rotation" in script
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")
    assert "cadsceneApplyPureRotationPose" in legacy
    assert 'apiPost("/api/pure-rotation/placement"' in script
    assert 'apiPost("/api/pure-rotation/corrections"' in script
    assert "pureRotationDeleteCorrection" in script
    server = Path("cadscene/cli/serve_viewer.py").read_text(encoding="utf-8")
    assert "global_camera_placement.json" in server
    assert "rotation_correction_keyframes.json" in server


def test_pure_rotation_viewer_can_focus_the_fixed_virtual_camera() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    assert 'id="pureRotationFocusCamera"' in html
    assert "cadsceneFocusVirtualCamera" in legacy
    assert 'document.querySelector("#pureRotationFocusCamera")' in workflow
    assert "focusPureRotationCameraOnce" in workflow
