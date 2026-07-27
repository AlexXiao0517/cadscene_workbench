from pathlib import Path


def test_viewer_has_isolated_pure_rotation_controls_and_no_sfm_path_contract() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    for identifier in ("pureRotationPanel", "pureRotationTrack", "pureRotationPlacement", "pureRotationCorrections"):
        assert identifier in html
    assert "/api/pure-rotation/trajectory" in script
    assert "pts_time_sec" in script
    assert "pure_rotation" in script
