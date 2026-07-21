from __future__ import annotations

from pathlib import Path
import re
import subprocess


APP_DIR = Path("apps/web_camera_viewer")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_web_viewer_static_files_exist() -> None:
    assert (APP_DIR / "index.html").exists()
    assert (APP_DIR / "paths.js").exists()
    assert (APP_DIR / "viewer_legacy.js").exists()
    assert (APP_DIR / "fallback.js").exists()
    assert (APP_DIR / "vendor").is_dir()


def test_web_viewer_files_do_not_default_to_old_out_or_project_paths() -> None:
    combined = "\n".join(_text(path) for path in (APP_DIR / "paths.js",))

    assert "proj" + "ect/" not in combined
    assert "../proj" + "ect" not in combined
    assert "cadvideo" not in combined
    assert "out/" not in combined
    assert "../../out" not in combined


def test_index_keeps_legacy_viewer_dom() -> None:
    text = _text(APP_DIR / "index.html")

    for token in ("sourceVideo", "overlayCanvas", "sceneContainer", "qualityTimelineCanvas", "sfmPanel", "cameraControls", "exportCamera"):
        assert token in text


def test_paths_js_supports_runs_outputs_and_url_overrides() -> None:
    text = _text(APP_DIR / "paths.js")

    for token in ("sfmScene", "qualityTimeline", "diagnosticsScene", "suggestions", "runId", "camera_track_pred.json", "track:", "param(params, \"track\")"):
        assert token in text
    assert "03_alignment" in text
    assert "04_quality" in text
    assert "05_viewer_scene" in text
    assert "06_road_surface" in text
    assert "videoFallbacks" in text
    assert "cadFallbacks" in text
    assert "/data/${encoded}/${encoded}.mp4" in text
    assert "/data/${encoded}/design.json" in text


def test_keyframes_use_authoritative_pts_and_source_frame_mapping() -> None:
    paths = _text(APP_DIR / "paths.js")
    viewer = _text(APP_DIR / "viewer_legacy.js")

    assert "frameTimestamps" in paths
    assert "frame_timestamps.csv" in paths
    assert "loadFrameTimestampsFromPath" in viewer
    assert "nearestFrameTimestamp" in viewer
    assert "pts_time_sec" in viewer
    assert "source_frame_index" in viewer
    assert "frame_mapping_source" in viewer


def test_viewer_legacy_contains_quality_suggestions_and_prediction_guards() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")

    assert "algorithm_prediction" in text
    assert "isManualKeyframe" in text
    assert "qualityTimeline" in text
    assert "loadSuggestions" in text
    assert "sfmScene" in text
    assert "diagnosticsScene" in text
    assert "0..255" in text or "255" in text
    assert "focusInspectOnCad" in text
    assert "120 / cadScale" in text
    assert "cadLineBuckets" in text
    assert "new THREE.LineSegments(geometry, material)" in text


def test_three_scene_scales_camera_model_for_cad_units() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")

    assert "cameraMarkerScale" in text
    assert "18 * cameraMarkerScale" in text
    assert "180 * cameraMarkerScale" in text
    assert "updateCameraVisualScale" in text
    assert "cameraVisualGroup.scale.setScalar" in text


def test_flat_cad_lines_are_not_hidden_by_the_ground_plane() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")

    assert "cadVisualLift" in text
    assert "cadGroup.position.y = cadVisualLift" in text
    assert "depthTest: false" in text
    assert "cadLine.renderOrder" in text


def test_viewer_focuses_the_primary_design_cluster_in_multi_sheet_cad() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")

    assert "CAD_FOCUS_LAYER_GROUPS" in text
    assert "function selectCadFocusBounds" in text
    assert "const focusBounds = selectCadFocusBounds(data)" in text
    assert "focusBounds.max_x - focusBounds.min_x" in text


def _script_sources() -> list[str]:
    html = _text(APP_DIR / "index.html")
    return re.findall(r'<script\s+src="([^"]+)"', html)


def test_index_vendor_scripts_exist_and_use_local_relative_paths() -> None:
    sources = _script_sources()
    vendor_sources = [src.split("?", 1)[0] for src in sources if "vendor/" in src]

    assert any("three" in src.lower() for src in vendor_sources)
    assert not any(src.startswith("../vendor") or src.startswith("/vendor") or src.startswith("/web_camera_viewer/vendor") for src in vendor_sources)
    for src in vendor_sources:
      assert (APP_DIR / src.lstrip("./")).exists(), src


def test_three_scripts_load_before_viewer_legacy() -> None:
    sources = _script_sources()
    normalized = [src.split("?", 1)[0] for src in sources]

    three_index = next(i for i, src in enumerate(normalized) if src.endswith("vendor/three.min.js") or src.endswith("vendor/three.module.js"))
    viewer_index = next(i for i, src in enumerate(normalized) if src.endswith("viewer_legacy.js"))
    fallback_index = next(i for i, src in enumerate(normalized) if src.endswith("fallback.js"))

    assert three_index < fallback_index < viewer_index


def test_three_controls_compatibility_and_debug_logs_exist() -> None:
    viewer = _text(APP_DIR / "viewer_legacy.js")
    compat = _text(APP_DIR / "vendor/controls/three-controls-compat.js")

    assert 'console.log("THREE available:"' in viewer
    assert "window.THREE.OrbitControls" in compat
    assert "window.THREE.TransformControls" in compat


def test_viewer_has_video_and_cad_fallback_loaders() -> None:
    viewer = _text(APP_DIR / "viewer_legacy.js")
    fallback = _text(APP_DIR / "fallback.js")

    assert "fetchJsonWithFallback" in viewer
    assert "bindVideoFallbacks" in viewer
    assert "CAD_FALLBACKS" in viewer
    assert "VIDEO_FALLBACKS" in viewer
    assert "Three.js 未加载" in fallback
    assert "viewer 初始化未完成" in fallback


def test_vendor_contains_three_file() -> None:
    assert any(path.name.startswith("three") and path.suffix == ".js" for path in (APP_DIR / "vendor").glob("*.js"))


def test_viewer_legacy_is_valid_javascript() -> None:
    result = subprocess.run(["node", "--check", str(APP_DIR / "viewer_legacy.js")], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
