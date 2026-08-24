from __future__ import annotations

from pathlib import Path


def test_application_resources_find_official_apps_and_configs() -> None:
    from cadscene.application_resources import application_root

    root = application_root()

    assert (root / "apps" / "workflow_portal" / "index.html").is_file()
    assert (root / "apps" / "project_workspace" / "index.html").is_file()
    assert (root / "apps" / "web_camera_viewer" / "index.html").is_file()
    assert (
        root / "configs" / "pipelines" / "sfm_overlay_existing_sfm.yaml"
    ).is_file()
    assert not (root / "apps" / "web_camera_viewer_broken_stage3f").exists()


def test_application_root_rejects_an_incomplete_installation(tmp_path: Path) -> None:
    from cadscene.application_resources import validate_application_root

    (tmp_path / "apps").mkdir()

    try:
        validate_application_root(tmp_path)
    except FileNotFoundError as exc:
        assert "configs" in str(exc)
    else:  # pragma: no cover - makes an unexpected success explicit
        raise AssertionError("incomplete application root was accepted")
