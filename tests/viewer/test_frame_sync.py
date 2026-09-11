from __future__ import annotations

import json
import subprocess
from pathlib import Path


MODULE = Path("apps/web_camera_viewer/frame_sync.js")


def _run(script_body: str) -> dict[str, object]:
    script = f"""
const api = require({json.dumps(str(MODULE.resolve()))});
{script_body}
"""
    completed = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_coordinator_rounds_clamps_and_notifies_logical_frame_immediately() -> None:
    result = _run(
        """
const logical = [];
const coordinator = api.createCoordinator({
  initialFrame: 5,
  clampFrame: (frame) => Math.max(0, Math.min(100, frame)),
  onLogicalFrame: (frame, origin) => logical.push([frame, origin]),
  seekVideo: () => { throw new Error("unexpected media seek"); },
});
coordinator.selectFrame(130.7, "video_seek");
process.stdout.write(JSON.stringify({ current: coordinator.currentFrame(), logical }));
"""
    )

    assert result == {"current": 100, "logical": [[100, "video_seek"]]}


def test_timeline_drag_coalesces_media_seek_and_flushes_exact_last_frame() -> None:
    result = _run(
        """
const logical = [];
const seeks = [];
let scheduled = null;
const coordinator = api.createCoordinator({
  initialFrame: 0,
  clampFrame: (frame) => Math.max(0, Math.min(1000, frame)),
  onLogicalFrame: (frame, origin) => logical.push([frame, origin]),
  seekVideo: (frame, origin) => seeks.push([frame, origin]),
  schedule: (callback) => { scheduled = callback; return 17; },
  cancel: () => { scheduled = null; },
});
coordinator.selectFrame(10, "timeline_drag", { media: "throttled" });
coordinator.selectFrame(20, "timeline_drag", { media: "throttled" });
const beforeFlush = seeks.slice();
coordinator.flushVideoSeek("timeline_release");
process.stdout.write(JSON.stringify({
  logical,
  beforeFlush,
  seeks,
  scheduledCleared: scheduled === null,
}));
"""
    )

    assert result["logical"] == [[10, "timeline_drag"], [20, "timeline_drag"]]
    assert result["beforeFlush"] == []
    assert result["seeks"] == [[20, "timeline_release"]]
    assert result["scheduledCleared"] is True


def test_video_origin_never_recursively_requests_a_media_seek() -> None:
    result = _run(
        """
const seeks = [];
const coordinator = api.createCoordinator({
  initialFrame: 0,
  clampFrame: (frame) => frame,
  onLogicalFrame: () => {},
  seekVideo: (frame, origin) => seeks.push([frame, origin]),
});
coordinator.selectFrame(12, "video_playback", { media: "immediate" });
coordinator.selectFrame(18, "video_seek", { media: "immediate" });
process.stdout.write(JSON.stringify({ current: coordinator.currentFrame(), seeks }));
"""
    )

    assert result == {"current": 18, "seeks": []}
