from __future__ import annotations

import pytest

from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import FrameTimestamp, resolve_frame_timestamps, sample_srt_at_frames


def _records() -> list[SrtRecord]:
    return [
        SrtRecord(start_sec=0.0, end_sec=0.1, latitude=30.0, longitude=120.0, altitude=10.0),
        SrtRecord(start_sec=1.0, end_sec=1.1, latitude=30.001, longitude=120.002, altitude=12.0),
    ]


def test_pts_by_source_frame_has_priority_over_cfr_fallback() -> None:
    rows = resolve_frame_timestamps(
        source_frame_indices=[0, 10],
        fps=29.97,
        pts_by_source_frame={0: 0.0, 10: 0.401},
        cfr_confirmed=False,
    )

    assert rows[1] == FrameTimestamp(10, 1, "frame_000010.png", 0.401, "pts_csv", False)


def test_cfr_fallback_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="cfr_confirmed"):
        resolve_frame_timestamps(source_frame_indices=[0], fps=25.0, cfr_confirmed=False)

    assert resolve_frame_timestamps(source_frame_indices=[25], fps=25.0, cfr_confirmed=True)[0].pts_time_sec == 1.0


def test_srt_sampling_interpolates_inside_small_gap_and_not_outside_coverage() -> None:
    frames = [
        FrameTimestamp(5, 0, "frame_000005.png", 0.5, "pts_csv", False),
        FrameTimestamp(20, 1, "frame_000020.png", 2.0, "pts_csv", False),
    ]
    samples = sample_srt_at_frames(_records(), frame_timestamps=frames, max_interpolation_gap_sec=1.5)

    assert samples[0].gps_valid is True
    assert samples[0].interpolated is True
    assert samples[0].latitude == pytest.approx(30.0005)
    assert samples[1].gps_valid is False
    assert samples[1].latitude is None


def test_time_offset_is_applied_to_srt_lookup_not_persisted_pts() -> None:
    frame = resolve_frame_timestamps(
        source_frame_indices=[10],
        fps=25.0,
        pts_by_source_frame={10: 0.0},
    )[0]

    sample = sample_srt_at_frames(
        _records(),
        frame_timestamps=[frame],
        max_interpolation_gap_sec=1.5,
        time_offset_sec=1.0,
    )[0]

    assert frame.pts_time_sec == 0.0
    assert sample.srt_time_sec == 1.0
    assert sample.latitude == pytest.approx(30.001)
