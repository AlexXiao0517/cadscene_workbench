from __future__ import annotations

import pytest

from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import FrameTimestamp, resolve_frame_timestamps, sample_srt_at_frames


def _records() -> list[SrtRecord]:
    return [
        SrtRecord(start_sec=0.0, end_sec=0.1, latitude=30.0, longitude=120.0, altitude=10.0),
        SrtRecord(start_sec=1.0, end_sec=1.1, latitude=30.001, longitude=120.002, altitude=12.0),
    ]


def _attitude_records(
    first_yaw: float = 10.0, second_yaw: float = 20.0
) -> list[SrtRecord]:
    return [
        SrtRecord(
            start_sec=0.0,
            end_sec=0.1,
            latitude=30.0,
            longitude=120.0,
            rel_alt=10.0,
            gimbal_yaw=first_yaw,
            gimbal_pitch=-40.0,
            gimbal_roll=1.0,
            drone_yaw=5.0,
            drone_pitch=1.0,
            drone_roll=-1.0,
        ),
        SrtRecord(
            start_sec=1.0,
            end_sec=1.1,
            latitude=30.001,
            longitude=120.002,
            rel_alt=12.0,
            gimbal_yaw=second_yaw,
            gimbal_pitch=-50.0,
            gimbal_roll=3.0,
            drone_yaw=15.0,
            drone_pitch=3.0,
            drone_roll=1.0,
        ),
    ]


def test_pts_by_source_frame_has_priority_over_cfr_fallback() -> None:
    rows = resolve_frame_timestamps(
        source_frame_indices=[0, 10],
        fps=29.97,
        pts_by_source_frame={0: 0.0, 10: 0.401},
        cfr_confirmed=False,
    )

    assert rows[1] == FrameTimestamp(10, 1, "frame_000010.png", 0.401, "pts_csv", False)


@pytest.mark.parametrize("time,valid", [(1.001, True), (1.099, True), (1.1, False), (1.2, False)])
def test_last_cue_is_valid_through_its_half_open_duration(time, valid):
    sample = sample_srt_at_frames(
        _attitude_records(),
        frame_timestamps=[FrameTimestamp(30, 30, "last.png", time, "pts_csv", False)],
        max_interpolation_gap_sec=1.5,
    )[0]
    assert sample.gps_valid is valid
    if valid:
        assert sample.rel_alt == 12
        assert sample.gimbal_yaw == 20
        assert sample.interpolated is False


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


def test_gimbal_and_drone_attitude_are_sampled_with_position() -> None:
    frame = FrameTimestamp(5, 0, "frame_000005.png", 0.5, "pts_csv", False)

    sample = sample_srt_at_frames(
        _attitude_records(),
        frame_timestamps=[frame],
        max_interpolation_gap_sec=1.0,
    )[0]

    assert sample.gimbal_yaw == pytest.approx(15.0)
    assert sample.gimbal_pitch == pytest.approx(-45.0)
    assert sample.gimbal_roll == pytest.approx(2.0)
    assert sample.drone_yaw == pytest.approx(10.0)
    assert sample.drone_pitch == pytest.approx(2.0)
    assert sample.drone_roll == pytest.approx(0.0)


def test_yaw_interpolation_wraps_across_180_on_the_short_arc() -> None:
    frame = FrameTimestamp(5, 0, "frame_000005.png", 0.5, "pts_csv", False)

    sample = sample_srt_at_frames(
        _attitude_records(179.0, -179.0),
        frame_timestamps=[frame],
        max_interpolation_gap_sec=1.0,
    )[0]

    assert sample.gimbal_yaw is not None
    assert abs(abs(sample.gimbal_yaw) - 180.0) < 1e-9


def test_long_gap_does_not_interpolate_attitude() -> None:
    records = _attitude_records()
    records[1] = SrtRecord(
        **{
            **records[1].to_dict(),
            "start_sec": 10.0,
            "end_sec": 10.1,
        }
    )
    frame = FrameTimestamp(5, 0, "frame_000005.png", 5.0, "pts_csv", False)

    sample = sample_srt_at_frames(
        records,
        frame_timestamps=[frame],
        max_interpolation_gap_sec=1.0,
    )[0]

    assert sample.gimbal_yaw is None
    assert sample.gimbal_pitch is None
    assert sample.drone_yaw is None
