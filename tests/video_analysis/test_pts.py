from __future__ import annotations

from fractions import Fraction
from pathlib import Path
import subprocess
from types import SimpleNamespace

import cadscene.video_analysis.pts as pts
import pytest
from cadscene.video_analysis.pts import (
    PacketTimestamp,
    choose_sparse_samples,
    decode_sparse_frames,
    iter_sparse_frames,
    parse_debug_packet_timestamps,
    parse_selected_video_time_base,
    probe_video_pts,
    resolve_ffmpeg_executable,
)


FRAME_JSON = {
    "frames": [
        {"pts": 5000, "best_effort_timestamp": 5000, "pkt_duration": 40},
        {"best_effort_timestamp": 5040, "pkt_duration": 40},
        {"pts": 5080, "best_effort_timestamp": 5080, "pkt_duration": 40},
    ]
}


def test_frame_index_prefers_pts_then_best_effort_in_presentation_order() -> None:
    frames = pts.parse_decoded_frame_records(
        FRAME_JSON, time_base=Fraction(1, 1000)
    )

    assert [frame.pts for frame in frames] == [5000, 5040, 5080]
    assert frames[1].timestamp_source == "best_effort_timestamp"


def test_source_end_exclusive_includes_last_frame() -> None:
    frames = tuple(
        pts.DecodedFrameTimestamp(
            ordinal=ordinal,
            pts=frame_pts,
            duration_pts=40,
            timestamp_source="pts",
        )
        for ordinal, frame_pts in enumerate((5000, 5040, 5080))
    )
    index = pts.DecodedFrameIndex(Fraction(1, 1000), frames)

    assert index.source_end_pts_exclusive == 5120
    assert index.frames[-1].pts < index.source_end_pts_exclusive


def test_frame_index_probe_uses_ffprobe_decoded_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "source.mkv"
    video.write_bytes(b"video")
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_bytes(b"probe")
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout='{"frames": [{"pts": "5000", "pkt_duration": "40"}]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)

    index = pts.probe_decoded_frame_index(
        video, time_base=Fraction(1, 1000), ffprobe_executable=ffprobe
    )

    assert index.source_start_pts == 5000
    assert "-show_frames" in calls[0]
    assert "frame=pts,best_effort_timestamp,pkt_duration" in calls[0]


def test_frame_index_probe_falls_back_to_decoded_ffmpeg_showinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "source.mkv"
    video.write_bytes(b"video")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"encoder")
    calls: list[list[str]] = []

    def unavailable(_: object) -> Path:
        raise RuntimeError("ffprobe unavailable")

    def run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr=(
                "[Parsed_showinfo_0] n: 0 pts: 5000 pts_time:5 "
                "duration: 40 duration_time:0.04\n"
                "[Parsed_showinfo_0] n: 1 pts: 5040 pts_time:5.04 "
                "duration: 40 duration_time:0.04\n"
            ),
        )

    monkeypatch.setattr(pts, "_resolve_ffprobe_executable", unavailable)
    monkeypatch.setattr(subprocess, "run", run)

    index = pts.probe_decoded_frame_index(
        video, time_base=Fraction(1, 1000), ffmpeg_executable=ffmpeg
    )

    assert [frame.pts for frame in index.frames] == [5000, 5040]
    assert "showinfo" in calls[0]
    assert "-copyts" in calls[0]


def test_packet_parser_preserves_irregular_source_pts_without_fps_math() -> None:
    debug_output = """
demuxer -> ist_index:0 type:video next_dts:NOPTS next_dts_time:NOPTS next_pts:NOPTS next_pts_time:NOPTS pkt_pts:9009 pkt_pts_time:0.1001 pkt_dts:9009 pkt_dts_time:0.1001 duration:3003 duration_time:0.0333667
demuxer -> ist_index:0 type:video next_dts:133466 next_dts_time:0.133466 next_pts:133466 next_pts_time:0.133466 pkt_pts:15015 pkt_pts_time:0.166833 pkt_dts:15015 pkt_dts_time:0.166833 duration:6006 duration_time:0.0667333
demuxer -> ist_index:0 type:video next_dts:233566 next_dts_time:0.233566 next_pts:233566 next_pts_time:0.233566 pkt_pts:45045 pkt_pts_time:0.5005 pkt_dts:45045 pkt_dts_time:0.5005 duration:3003 duration_time:0.0333667
"""

    packets = parse_debug_packet_timestamps(debug_output, time_base=Fraction(1, 90_000))

    assert [packet.pts for packet in packets] == [9009, 15015, 45045]
    assert [packet.pts_sec for packet in packets] == [0.1001, 0.16683333333333333, 0.5005]
    assert packets[1].duration_sec == 0.06673333333333334


def test_sparse_selection_uses_packet_pts_and_always_keeps_ends() -> None:
    packets = [
        PacketTimestamp(pts=0, pts_sec=0.0, duration_sec=0.1),
        PacketTimestamp(pts=1, pts_sec=0.1, duration_sec=0.3),
        PacketTimestamp(pts=4, pts_sec=0.4, duration_sec=0.6),
        PacketTimestamp(pts=10, pts_sec=1.0, duration_sec=0.1),
    ]

    samples = choose_sparse_samples(packets, interval_sec=0.35)

    assert [sample.pts_sec for sample in samples] == [0.0, 0.4, 1.0]


def test_packet_parser_rejects_non_monotonic_source_pts() -> None:
    debug_output = """
demuxer -> ist_index:0 type:video pkt_pts:100 pkt_pts_time:1.0 duration:10 duration_time:0.1
demuxer -> ist_index:0 type:video pkt_pts:90 pkt_pts_time:0.9 duration:10 duration_time:0.1
"""

    try:
        parse_debug_packet_timestamps(debug_output, time_base=Fraction(1, 100))
    except ValueError as exc:
        assert "non-monotonic" in str(exc)
    else:
        raise AssertionError("non-monotonic PTS must be reported")


def test_probe_reads_irregular_pts_from_real_vfr_video(tmp_path: Path) -> None:
    ffmpeg = resolve_ffmpeg_executable()
    video = tmp_path / "irregular.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=0.4",
            "-vf",
            "setpts=N*N",
            "-frames:v",
            "4",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            str(video),
        ],
        check=True,
    )

    index = probe_video_pts(video, ffmpeg_executable=ffmpeg)

    assert index.time_base == Fraction(1, 1000)
    assert [packet.pts_sec for packet in index.packets] == [0.0, 0.1, 0.4, 0.9]
    assert index.source_start_pts_sec == 0.0
    assert index.source_end_pts_sec == 0.9

    frames = decode_sparse_frames(
        video,
        index=index,
        interval_sec=0.25,
        output_size=(64, 48),
        ffmpeg_executable=ffmpeg,
    )

    assert [frame.pts_sec for frame in frames] == [0.0, 0.4, 0.9]
    assert all(frame.image.shape == (48, 64) for frame in frames)

    streamed = list(
        iter_sparse_frames(
            video,
            index=index,
            interval_sec=0.25,
            output_size=(64, 48),
            ffmpeg_executable=ffmpeg,
        )
    )
    assert [frame.pts_sec for frame in streamed] == [0.0, 0.4, 0.9]


def test_sparse_decode_preserves_nonzero_absolute_frame_pts(tmp_path: Path) -> None:
    ffmpeg = resolve_ffmpeg_executable()
    video = tmp_path / "nonzero.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=0.4",
            "-vf",
            "setpts=PTS+5/TB",
            "-copyts",
            "-c:v",
            "ffv1",
            str(video),
        ],
        check=True,
    )
    frame_index = pts.probe_decoded_frame_index(
        video, ffmpeg_executable=ffmpeg
    )

    frames = decode_sparse_frames(
        video,
        index=frame_index,
        interval_sec=0.2,
        output_size=(64, 48),
        ffmpeg_executable=ffmpeg,
    )

    assert frame_index.source_start_pts == 5000
    assert [frame.pts for frame in frames] == [5000, 5200]


def test_time_base_is_bound_to_selected_video_not_first_video_or_container_stream() -> None:
    ffmpeg_output = """
  Stream #0:0: Audio: aac, 48000 Hz, stereo
  Stream #0:1: Video: mjpeg, yuvj420p, 960x540, 90k tbr, 90k tbn (attached pic)
  Stream #0:2: Video: hevc, yuv420p, 3840x2160, 29.97 fps, 30k tbn
Stream mapping:
  Stream #0:2 -> #0:0 (copy)
"""

    assert parse_selected_video_time_base(ffmpeg_output) == Fraction(1, 30_000)
