from __future__ import annotations

import json
import subprocess
from pathlib import Path


MODULE = Path("apps/web_camera_viewer/annotation_pts.js")


def _node(expression: str):
    script = (
        f"const m=require({json.dumps(str(MODULE.resolve()))}); "
        f"console.log(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_preview_time_lookup_selects_neighboring_authoritative_irregular_pts() -> None:
    frames = [
        {"source_pts": 100, "clip_time_sec": 0.0},
        {"source_pts": 104, "clip_time_sec": 0.04},
        {"source_pts": 111, "clip_time_sec": 0.11},
    ]

    assert _node(f"m.sourcePtsAtTime({json.dumps(frames)}, 0.019)") == 100
    assert _node(f"m.sourcePtsAtTime({json.dumps(frames)}, 0.021)") == 104
    assert _node(f"m.sourcePtsAtTime({json.dumps(frames)}, 0.08)") == 111


def test_video_tracking_visual_never_freezes_or_interpolates_across_lost() -> None:
    results = [
        {
            "source_pts": 100,
            "anchor_xy": [40, 30],
            "bbox": [20, 20, 40, 20],
            "confidence": 0.9,
            "visibility": True,
            "tracking_status": "tracked",
        },
        {
            "source_pts": 104,
            "anchor_xy": None,
            "bbox": None,
            "confidence": 0.0,
            "visibility": False,
            "tracking_status": "lost",
        },
    ]
    annotation = {
        "screen_offset": [10, -5],
        "visibility_policy": {"min_tracking_confidence": 0.5},
    }
    transform = "(point) => ({x:point.x*2,y:point.y*2})"

    visible = _node(
        f"m.videoTrackVisual({json.dumps(annotation)}, {json.dumps(results)}, 100, {transform})"
    )
    lost = _node(
        f"m.videoTrackVisual({json.dumps(annotation)}, {json.dumps(results)}, 104, {transform})"
    )
    missing_after_lost = _node(
        f"m.videoTrackVisual({json.dumps(annotation)}, {json.dumps(results)}, 111, {transform})"
    )

    assert visible["visible"] is True
    assert visible["anchor_xy"] == [80, 60]
    assert visible["label_xy"] == [100, 50]
    assert lost == {"visible": False, "reason": "tracking_lost", "tracking_status": "lost", "confidence": 0}
    assert missing_after_lost == {"visible": False, "reason": "no_exact_tracking_pts"}


def test_untracked_video_callout_is_visible_only_on_its_initialization_pts() -> None:
    annotation = {
        "active_tracking_revision": None,
        "screen_offset": [80, -40],
        "anchor": {
            "initialization": {
                "source_pts": 100,
                "bbox": [20, 30, 40, 20],
            }
        },
        "visibility_policy": {"min_tracking_confidence": 0.5},
    }
    transform = "(point) => ({x:point.x,y:point.y})"

    draft = _node(
        f"m.videoTrackVisual({json.dumps(annotation)}, [], 100, {transform})"
    )
    other_frame = _node(
        f"m.videoTrackVisual({json.dumps(annotation)}, [], 104, {transform})"
    )

    assert draft["visible"] is True
    assert draft["anchor_xy"] == [40, 40]
    assert draft["label_xy"] == [120, 0]
    assert draft["tracking_status"] == "untracked"
    assert other_frame == {"visible": False, "reason": "no_exact_tracking_pts"}


def test_video_tracking_visual_uses_constant_time_pts_index() -> None:
    result = {
        "source_pts": 104,
        "anchor_xy": [40, 30],
        "confidence": 0.9,
        "visibility": True,
        "tracking_status": "tracked",
    }
    annotation = {
        "screen_offset": [0, 0],
        "visibility_policy": {"min_tracking_confidence": 0.5},
    }

    visual = _node(
        "m.videoTrackVisual("
        f"{json.dumps(annotation)}, new Map([[104, {json.dumps(result)}]]), 104, "
        "(point) => point)"
    )

    assert visual["visible"] is True
    assert visual["anchor_xy"] == [40, 30]
