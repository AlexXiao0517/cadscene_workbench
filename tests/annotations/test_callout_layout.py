from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cadscene.annotations.callout_layout import layout_callout


JS_MODULE = Path("apps/web_camera_viewer/callout_layout.js")


def _browser_layout(payload: dict[str, object]) -> dict[str, object]:
    script = (
        f"const m=require({json.dumps(str(JS_MODULE.resolve()))});"
        f"console.log(JSON.stringify(m.layoutCallout({json.dumps(payload)})));"
    )
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(completed.stdout)


def test_callout_panel_is_clamped_and_leader_uses_nearest_edge() -> None:
    layout = layout_callout(
        anchor_xy=(100.0, 220.0),
        screen_offset=(300.0, -120.0),
        panel_size=(200.0, 80.0),
        viewport_size=(640.0, 360.0),
        safe_margin=20.0,
        elbow_length=24.0,
    )

    assert layout.panel_rect == (300.0, 60.0, 200.0, 80.0)
    assert layout.leader_points == (
        (100.0, 220.0),
        (276.0, 140.0),
        (300.0, 140.0),
    )


def test_callout_panel_stays_inside_video_safe_area() -> None:
    layout = layout_callout(
        anchor_xy=(620.0, 20.0),
        screen_offset=(100.0, -100.0),
        panel_size=(240.0, 100.0),
        viewport_size=(640.0, 360.0),
        safe_margin=20.0,
        elbow_length=20.0,
    )

    assert layout.panel_rect == (380.0, 20.0, 240.0, 100.0)
    assert layout.leader_points[-1] == (620.0, 20.0)


def test_browser_and_renderer_share_callout_layout_semantics() -> None:
    payload = {
        "anchor_xy": [100.0, 220.0],
        "screen_offset": [300.0, -120.0],
        "panel_size": [200.0, 80.0],
        "viewport_size": [640.0, 360.0],
        "safe_margin": 20.0,
        "elbow_length": 24.0,
    }

    python_layout = layout_callout(
        anchor_xy=tuple(payload["anchor_xy"]),
        screen_offset=tuple(payload["screen_offset"]),
        panel_size=tuple(payload["panel_size"]),
        viewport_size=tuple(payload["viewport_size"]),
        safe_margin=float(payload["safe_margin"]),
        elbow_length=float(payload["elbow_length"]),
    ).to_dict()

    assert _browser_layout(payload) == python_layout


def test_oversized_safe_margin_never_inverts_viewport_bounds() -> None:
    payload = {
        "anchor_xy": [100.0, 50.0],
        "screen_offset": [0.0, 0.0],
        "panel_size": [160.0, 80.0],
        "viewport_size": [200.0, 100.0],
        "safe_margin": 160.0,
        "elbow_length": 0.0,
    }
    layout = layout_callout(
        anchor_xy=(100.0, 50.0),
        screen_offset=(0.0, 0.0),
        panel_size=(160.0, 80.0),
        viewport_size=(200.0, 100.0),
        safe_margin=160.0,
        elbow_length=0.0,
    )

    left, top, width, height = layout.panel_rect
    assert 0 <= left <= left + width <= 200
    assert 0 <= top <= top + height <= 100
    assert _browser_layout(payload) == layout.to_dict()
