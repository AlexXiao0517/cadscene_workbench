from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

from cadscene.annotations.tracking import decode_tracking_frames
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp


def test_tracking_decode_zips_pixels_to_authoritative_irregular_source_pts(
    tmp_path: Path,
) -> None:
    video = tmp_path / "tracking.avi"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"MJPG"), 25.0, (64, 48)
    )
    assert writer.isOpened()
    for value in (20, 60, 100, 140):
        writer.write(np.full((48, 64, 3), value, dtype=np.uint8))
    writer.release()
    index = DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, None, "pts")
            for ordinal, pts in enumerate((100, 141, 205, 260))
        ),
    )

    decoded = decode_tracking_frames(
        video,
        source_start_pts=141,
        source_end_pts_exclusive=260,
        expected_time_base=Fraction(1, 1000),
        frame_index=index,
    )

    assert tuple(frame.source_pts for frame in decoded) == (141, 205)
    assert abs(float(decoded[0].image_bgr.mean()) - 60.0) < 3.0
    assert abs(float(decoded[1].image_bgr.mean()) - 100.0) < 3.0
