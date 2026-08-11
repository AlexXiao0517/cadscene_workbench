from __future__ import annotations

import pytest

from cadscene.projects.http_api import _visible_job_progress


@pytest.mark.parametrize(
    ("status", "progress", "expected"),
    [
        ("queued", None, 0.0),
        (
            "queued",
            {"stage": "rendering_frames", "message": "previous attempt", "fraction": 0.95},
            0.0,
        ),
        (
            "preparing",
            {"stage": "rendering_frames", "message": "previous attempt", "fraction": 0.95},
            0.0,
        ),
        ("running", {"stage": "running", "message": "running"}, 0.0),
        (
            "running",
            {"stage": "rendering_frames", "message": "frame 25/100", "fraction": 0.25},
            0.25,
        ),
        (
            "validating",
            {"stage": "rendering_frames", "message": "frame 100/100", "fraction": 0.95},
            0.99,
        ),
    ],
)
def test_clip_render_visible_progress_uses_render_measurement_not_prerequisite_completion(
    status: str,
    progress: dict[str, object] | None,
    expected: float,
) -> None:
    visible = _visible_job_progress(
        {
            "job_type": "clip_render",
            "status": status,
            "stage": status,
            "progress": progress,
        }
    )

    assert visible is not None
    assert visible["fraction"] == expected
