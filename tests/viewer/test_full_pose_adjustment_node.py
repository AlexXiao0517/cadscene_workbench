from __future__ import annotations

import json
from pathlib import Path
import subprocess


MODULE = Path("apps/web_camera_viewer/full_pose_adjustment.js")


def _run(expression: str) -> object:
    script = (
        f"const api=require({json.dumps(str(MODULE.resolve()))});"
        f"process.stdout.write(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_absolute_offset_translates_every_position_by_only_the_delta() -> None:
    track = {
        "fps": 25,
        "meta": {"workflow": "srt_full_pose", "cad_scale": 0.5},
        "keyframes": [
            {
                "frame": 0,
                "source": "srt_full_pose",
                "camera": {
                    "x": 10,
                    "y": 20,
                    "z": 30,
                    "yaw": 40,
                    "pitch": -50,
                    "roll": 6,
                    "fov": 59.109,
                },
            },
            {
                "frame": 1,
                "source": "srt_full_pose",
                "camera": {
                    "x": 11,
                    "y": 21,
                    "z": 31,
                    "yaw": 41,
                    "pitch": -51,
                    "roll": 7,
                    "fov": 59.109,
                },
            },
        ],
    }
    expression = (
        "api.applyAbsoluteOffset("
        f"{json.dumps(track)},{{x:1,y:2,z:3}},{{x:4,y:6,z:8}})"
    )

    result = _run(expression)

    assert result["offset"] == {"x": 4, "y": 6, "z": 8}
    assert result["track"]["keyframes"][0]["camera"] == {
        "x": 16,
        "y": 28,
        "z": 40,
        "yaw": 40,
        "pitch": -50,
        "roll": 6,
        "fov": 59.109,
    }
    assert result["track"]["keyframes"][1]["camera"]["x"] == 17
    assert result["track"]["meta"]["route_offset_xyz_m"] == [4, 6, 8]
    assert result["track"]["meta"]["authoritative_workbench_track"] is True
    assert track["keyframes"][0]["camera"]["x"] == 10


def test_absolute_offset_rejects_non_finite_values() -> None:
    script = (
        f"const api=require({json.dumps(str(MODULE.resolve()))});"
        "try { api.applyAbsoluteOffset({keyframes:[]},{x:0,y:0,z:0},"
        "{x:Number.NaN,y:0,z:0}); process.stdout.write('missed'); }"
        "catch (error) { process.stdout.write(error.message); }"
    )
    completed = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )

    assert "finite" in completed.stdout


def test_normalize_offset_reads_saved_track_metadata() -> None:
    result = _run(
        "api.offsetFromTrack({meta:{route_offset_xyz_m:[1.25,-2,3]}})"
    )

    assert result == {"x": 1.25, "y": -2, "z": 3}
