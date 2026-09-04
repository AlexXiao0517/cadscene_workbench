from __future__ import annotations

import json
import subprocess
from pathlib import Path


MODULE = Path("apps/web_camera_viewer/keyframe_sources.js")


def _is_confirmed(item: dict) -> bool:
    script = (
        f"const m=require({json.dumps(str(MODULE.resolve()))});"
        f"process.stdout.write(JSON.stringify(m.isConfirmedManualKeyframe({json.dumps(item)})));"
    )
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_only_explicit_manual_sources_are_confirmed() -> None:
    for source in (
        "manual_keyframe",
        "confirmed_keyframe",
        "manual",
        "confirmed",
        "manual_anchor",
        "manual_corrected",
        "scene_boundary_anchor",
        "scene_overlap_anchor",
    ):
        assert _is_confirmed({"frame": 10, "source": source, "camera": {"x": 1}})

    assert not _is_confirmed({"frame": 0, "camera": {"x": 100}})
    assert not _is_confirmed({"frame": 10, "source": "algorithm_prediction", "camera": {"x": 1}})
    assert not _is_confirmed({"frame": 10, "source": "unknown", "camera": {"x": 1}})


def test_legacy_source_less_track_requires_multiple_poses() -> None:
    module_path = json.dumps(str(MODULE.resolve()))
    single = json.dumps([{"frame": 0, "camera": {"x": 100}}])
    legacy = json.dumps(
        [
            {"frame": 0, "camera": {"x": 0}},
            {"frame": 10, "camera": {"x": 1}},
        ]
    )
    script = (
        f"const m=require({module_path});"
        f"process.stdout.write(JSON.stringify([m.confirmedManualKeyframes({single}).length,"
        f"m.confirmedManualKeyframes({legacy}).length]));"
    )
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)

    assert json.loads(completed.stdout) == [0, 2]


def test_keyframe_source_module_loads_before_viewer_and_workflow() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")

    module_index = html.index("keyframe_sources.js")
    assert module_index < html.index("viewer_legacy.js")
    assert module_index < html.index("workflow.js")
    viewer = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(
        encoding="utf-8"
    )
    assert "mergeFittedTrackWithManual" in viewer


def _merge_tracks(fitted: dict | None, manual: dict | None) -> dict | None:
    script = (
        f"const m=require({json.dumps(str(MODULE.resolve()))});"
        f"process.stdout.write(JSON.stringify(m.mergeFittedTrackWithManual("
        f"{json.dumps(fitted)},{json.dumps(manual)})));"
    )
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_manual_keyframes_override_fitted_frames_and_append_new_anchors() -> None:
    fitted = {
        "version": 1,
        "fps": 25,
        "keyframes": [
            {"frame": 0, "source": "algorithm_prediction", "camera": {"x": 0}},
            {"frame": 10, "source": "algorithm_prediction", "camera": {"x": 10}},
        ],
    }
    manual = {
        "version": 1,
        "fps": 25,
        "keyframes": [
            {"frame": 10, "source": "manual_keyframe", "camera": {"x": 101}},
            {"frame": 15, "source": "manual_keyframe", "camera": {"x": 151}},
        ],
    }

    merged = _merge_tracks(fitted, manual)

    assert [item["frame"] for item in merged["keyframes"]] == [0, 10, 15]
    assert merged["keyframes"][1]["camera"]["x"] == 101
    assert merged["keyframes"][1]["source"] == "manual_keyframe"
    assert merged["keyframes"][2]["camera"]["x"] == 151


def test_non_manual_fallback_pose_does_not_override_fitted_track() -> None:
    fitted = {
        "keyframes": [
            {"frame": 10, "source": "algorithm_prediction", "camera": {"x": 10}}
        ]
    }
    fallback = {
        "keyframes": [
            {"frame": 10, "source": "algorithm_prediction", "camera": {"x": 999}}
        ]
    }

    merged = _merge_tracks(fitted, fallback)

    assert merged["keyframes"][0]["camera"]["x"] == 10


def test_track_merge_keeps_single_available_payload_compatible() -> None:
    fitted = {"fps": 30, "keyframes": [{"frame": 3, "camera": {"x": 3}}]}
    manual = {"fps": 25, "keyframes": [{"frame": 4, "camera": {"x": 4}}]}

    assert _merge_tracks(fitted, None) == fitted
    assert _merge_tracks(None, manual) == manual
    assert _merge_tracks({"keyframes": []}, manual) == manual
    assert _merge_tracks(None, None) is None


def test_authoritative_full_pose_workbench_track_replaces_initial_track() -> None:
    fitted = {
        "meta": {"workflow": "srt_full_pose"},
        "keyframes": [
            {"frame": 0, "source": "srt_full_pose", "camera": {"x": 10}}
        ],
    }
    manual = {
        "meta": {
            "workflow": "srt_full_pose",
            "authoritative_workbench_track": True,
            "route_offset_xyz_m": [3, 0, 0],
        },
        "keyframes": [
            {"frame": 0, "source": "srt_full_pose", "camera": {"x": 13}}
        ],
    }

    merged = _merge_tracks(fitted, manual)

    assert merged == manual
