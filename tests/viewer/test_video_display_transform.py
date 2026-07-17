from __future__ import annotations

import json
import subprocess
from pathlib import Path


MODULE = Path("apps/web_camera_viewer/video_display_transform.js")


def _run_transform(video_width: int, video_height: int, container_width: int, container_height: int) -> dict:
    script = f"""
const {{ VideoDisplayTransform }} = require({json.dumps(str(MODULE.resolve()))});
const transform = new VideoDisplayTransform({video_width}, {video_height}, {container_width}, {container_height});
const center = transform.sourceToDisplay({{ x: {video_width / 2}, y: {video_height / 2} }});
const roundTrip = transform.displayToSource(center);
const blackBar = transform.displayToSource({{ x: 1, y: 1 }});
process.stdout.write(JSON.stringify({{
  scale: transform.scale,
  displayWidth: transform.displayWidth,
  displayHeight: transform.displayHeight,
  offsetX: transform.offsetX,
  offsetY: transform.offsetY,
  center,
  roundTrip,
  blackBar,
}}));
"""
    result = subprocess.run(["node", "-e", script], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_16_by_9_and_4k_use_the_same_fixed_container_geometry() -> None:
    hd = _run_transform(1920, 1080, 960, 540)
    uhd = _run_transform(3840, 2160, 960, 540)

    assert hd["displayWidth"] == uhd["displayWidth"] == 960
    assert hd["displayHeight"] == uhd["displayHeight"] == 540
    assert hd["offsetX"] == uhd["offsetX"] == 0
    assert hd["offsetY"] == uhd["offsetY"] == 0


def test_4_by_3_video_uses_horizontal_letterbox_and_rejects_black_bar_clicks() -> None:
    transform = _run_transform(1440, 1080, 960, 540)

    assert transform["displayWidth"] == 720
    assert transform["displayHeight"] == 540
    assert transform["offsetX"] == 120
    assert transform["offsetY"] == 0
    assert transform["blackBar"] is None
    assert transform["roundTrip"] == {"x": 720, "y": 540}


def test_portrait_video_is_contained_without_resizing_the_panel() -> None:
    transform = _run_transform(1080, 1920, 960, 540)

    assert transform["displayHeight"] == 540
    assert transform["displayWidth"] == 303.75
    assert transform["offsetX"] == 328.125
    assert transform["offsetY"] == 0
    assert transform["blackBar"] is None


def test_viewer_uses_one_transform_for_overlay_and_pointer_coordinates() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    assert "video_display_transform.js" in html
    assert html.index("video_display_transform.js") < html.index("viewer_legacy.js")
    assert "new window.VideoDisplayTransform" in script
    assert "sourceToDisplay" in script
    assert "displayToSource" in script
    assert "ResizeObserver" in script
    assert "video.videoWidth" in script and "video.videoHeight" in script
