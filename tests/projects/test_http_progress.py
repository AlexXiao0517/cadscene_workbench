from __future__ import annotations

import pytest

from cadscene.projects.http_api import _visible_job_progress


@pytest.mark.parametrize(
    ("status", "progress", "expected"),
    [
        ("queued", {"stage": "queued", "message": "queued"}, 0.0),
        ("preparing", {"stage": "preparing", "message": "preparing"}, 0.0),
        ("running", {"stage": "running", "message": "running"}, 0.0),
        (
            "running",
            {"stage": "rendering_frames", "message": "frame 25/100", "fraction": 0.25},
            0.25,
        ),
        ("validating", {"stage": "validating", "message": "validating"}, 0.99),
    ],
)
def test_clip_render_visible_progress_uses_render_measurement_not_prerequisite_completion(
    status: str,
    progress: dict[str, object],
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
