from __future__ import annotations

import cv2
import numpy as np
import pytest

from cadscene.annotations.tracking import (
    OpenCvLkVideoAnchorTracker,
    TrackingResult,
    TrackingFrame,
    VideoTrackingInitialization,
)


def test_lost_tracking_result_cannot_claim_visibility() -> None:
    with pytest.raises(ValueError, match="lost tracking results must be hidden"):
        TrackingResult(
            source_pts=100,
            bbox=(10.0, 10.0, 20.0, 20.0),
            anchor_xy=(20.0, 20.0),
            confidence=0.8,
            visible=True,
            tracking_status="lost",
            diagnostic="inconsistent payload",
        )


def _target_frame(x: int | None) -> np.ndarray:
    image = np.zeros((100, 160, 3), dtype=np.uint8)
    if x is None:
        return image
    for row in range(3):
        for column in range(4):
            center = (x + 5 + column * 8, 35 + 5 + row * 8)
            cv2.circle(image, center, 2, (255, 255, 255), -1)
    cv2.rectangle(image, (x, 35), (x + 36, 63), (180, 180, 180), 1)
    return image


def test_lk_tracker_tracks_forward_and_backward_by_authoritative_source_pts() -> None:
    pts = (100, 103, 107, 112, 118)
    frames = tuple(
        TrackingFrame(source_pts=value, image_bgr=_target_frame(30 + index * 3))
        for index, value in enumerate(pts)
    )
    tracker = OpenCvLkVideoAnchorTracker(min_features=4)

    results = tracker.track(
        frames,
        VideoTrackingInitialization(source_pts=107, bbox=(36.0, 35.0, 36.0, 28.0)),
    )

    assert tuple(item.source_pts for item in results) == pts
    assert all(item.visible for item in results)
    assert results[2].tracking_status == "initialized"
    assert abs(results[0].anchor_xy[0] - 48.0) < 1.5
    assert abs(results[-1].anchor_xy[0] - 60.0) < 1.5
    assert all(0.0 <= item.confidence <= 1.0 for item in results)


def test_lost_target_hides_immediately_and_never_freezes_last_position() -> None:
    frames = (
        TrackingFrame(100, _target_frame(30)),
        TrackingFrame(104, _target_frame(34)),
        TrackingFrame(109, _target_frame(None)),
        TrackingFrame(115, _target_frame(None)),
    )
    tracker = OpenCvLkVideoAnchorTracker(min_features=4)

    results = tracker.track(
        frames,
        VideoTrackingInitialization(source_pts=100, bbox=(30.0, 35.0, 36.0, 28.0)),
    )

    assert results[0].visible is True
    assert results[1].visible is True
    assert results[2].visible is False
    assert results[2].tracking_status == "lost"
    assert results[2].anchor_xy is None
    assert results[2].bbox is None
    assert results[3].visible is False
    assert results[3].tracking_status == "lost"
    assert results[3].anchor_xy is None
    assert "prior" in results[3].diagnostic
