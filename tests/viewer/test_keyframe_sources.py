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
