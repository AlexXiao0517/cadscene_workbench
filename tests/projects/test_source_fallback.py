from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import pytest

import cadscene.projects.source_fallback as source_fallback
from cadscene.cli.render_source_interval import _positive_fraction
from cadscene.projects.media import ProjectMediaSpec, parse_ffprobe, probe_media
from cadscene.projects.service import ProjectService
from cadscene.projects.source_fallback import (
    SourceIntervalRenderInputs,
    build_source_interval_ffmpeg_command,
    build_source_render_frame_map,
    render_source_interval,
    SourceIntervalRenderAdapter,
)
from cadscene.video_analysis.pts import (
    DecodedFrameIndex,
    DecodedFrameTimestamp,
    probe_decoded_frame_index,
    resolve_ffmpeg_executable,
)


def _media_spec() -> ProjectMediaSpec:
    return ProjectMediaSpec(
        width=320,
        height=180,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 1000),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=None,
    )


def _index() -> DecodedFrameIndex:
    return DecodedFrameIndex(
        Fraction(1, 1000),
        (
            DecodedFrameTimestamp(0, 5000, 40, "pts"),
            DecodedFrameTimestamp(1, 5040, 50, "pts"),
            DecodedFrameTimestamp(2, 5090, 40, "best_effort_timestamp"),
            DecodedFrameTimestamp(3, 5130, 60, "pts"),
        ),
    )


def _inputs(tmp_path: Path) -> SourceIntervalRenderInputs:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"source")
    attempt = (tmp_path / "attempt-1").resolve()
    attempt.mkdir()
    return SourceIntervalRenderInputs(
        project_id="project-1",
        clip_id="clip-1",
        source_video_path=source,
        source_start_pts=5040,
        source_end_pts_exclusive=5130,
        source_time_base=Fraction(1, 1000),
        attempt_directory=attempt,
        project_media_spec=_media_spec(),
    )


def test_source_frame_map_uses_decoded_order_and_integer_pts_half_open() -> None:
    payload = build_source_render_frame_map(
        _index(),
        clip_id="clip-1",
        source_start_pts=5040,
        source_end_pts_exclusive=5130,
        source_time_base=Fraction(1, 1000),
    )

    assert payload["interval_semantics"] == "half_open"
    assert payload["source_time_base"] == {"numerator": 1, "denominator": 1000}
    assert payload["source_start_pts"] == 5040
    assert payload["source_end_pts_exclusive"] == 5130
    assert payload["frames"] == [
        {
            "output_frame_ordinal": 0,
            "source_decoded_frame_ordinal": 1,
            "source_pts": 5040,
        },
        {
            "output_frame_ordinal": 1,
            "source_decoded_frame_ordinal": 2,
            "source_pts": 5090,
        },
    ]


def test_source_frame_map_fails_when_time_base_or_interval_is_unreliable() -> None:
    with pytest.raises(ValueError, match="time base"):
        build_source_render_frame_map(
            _index(),
            clip_id="clip-1",
            source_start_pts=5040,
            source_end_pts_exclusive=5130,
            source_time_base=Fraction(1, 90000),
        )
    with pytest.raises(ValueError, match="decoded frames"):
        build_source_render_frame_map(
            _index(),
            clip_id="clip-1",
            source_start_pts=7000,
            source_end_pts_exclusive=7100,
            source_time_base=Fraction(1, 1000),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_start_pts", True),
        ("source_start_pts", 5040.0),
        ("source_end_pts_exclusive", False),
        ("source_end_pts_exclusive", 5130.0),
        ("source_time_base", 0.001),
    ],
)
def test_source_interval_inputs_reject_noninteger_pts_and_inexact_time_base(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    valid = _inputs(tmp_path)

    with pytest.raises(ValueError):
        SourceIntervalRenderInputs(**{**valid.__dict__, field: value})


@pytest.mark.parametrize(
    "value",
    ("0.001", "1", "true", "0/1000", "1/0", "-1/1000", "-1/-1000"),
)
def test_source_interval_cli_requires_positive_numerator_denominator_time_base(
    value: str,
) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="positive rational"):
        _positive_fraction(value)


def test_source_interval_cli_parses_exact_rational_time_base() -> None:
    assert _positive_fraction("1/1000") == Fraction(1, 1000)


def test_ffmpeg_source_interval_command_is_absolute_pts_passthrough_video_only(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    command = build_source_interval_ffmpeg_command(
        inputs,
        ffmpeg_executable=Path("C:/ffmpeg/bin/ffmpeg.exe"),
    )

    assert "-copyts" in command
    assert "-ss" not in command and "-t" not in command
    assert "-r" not in command and "-framerate" not in command
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert "-an" in command
    assert "trim=start_pts=5040:end_pts=5130,setpts=PTS-STARTPTS" in command[
        command.index("-vf") + 1
    ]
    assert command[command.index("-i") + 1] == str(inputs.source_video_path)
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-bf") + 1] == "0"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-video_track_timescale") + 1] == "1000"


def test_render_writes_authoritative_frame_map_before_starting_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    ffmpeg = (tmp_path / "ffmpeg.exe").resolve()
    ffmpeg.write_bytes(b"exe")

    def fake_run(command: tuple[str, ...], **_kwargs: object) -> object:
        frame_map_path = inputs.attempt_directory / "render_frame_map.json"
        assert frame_map_path.is_file()
        payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
        assert [item["source_pts"] for item in payload["frames"]] == [5040, 5090]
        assert "-ss" not in command and "-t" not in command and "-r" not in command
        Path(command[-1]).write_bytes(b"encoded")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(
        "cadscene.projects.source_fallback.probe_decoded_frame_index",
        lambda *_args, **_kwargs: _index(),
    )
    monkeypatch.setattr(
        "cadscene.projects.source_fallback.subprocess.run", fake_run,
    )
    monkeypatch.setattr(
        "cadscene.projects.source_fallback._validate_files",
        lambda *_args, **_kwargs: {},
    )

    video, frame_map = render_source_interval(inputs, ffmpeg_executable=ffmpeg)

    assert video.read_bytes() == b"encoded"
    assert frame_map.is_file()


def test_atomic_frame_map_write_does_not_follow_precreated_fixed_temp_symlink(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "render_frame_map.json"
    legacy_temporary = tmp_path / ".render_frame_map.json.tmp"
    external = tmp_path / "external.json"
    external.write_text("sentinel", encoding="utf-8")
    try:
        legacy_temporary.symlink_to(external)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    source_fallback._write_json_atomic(destination, {"writer": "safe"})

    assert json.loads(destination.read_text(encoding="utf-8")) == {"writer": "safe"}
    assert external.read_text(encoding="utf-8") == "sentinel"
    assert legacy_temporary.is_symlink()


def test_atomic_frame_map_writers_use_unique_temps_and_leave_residual_untouched(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "render_frame_map.json"
    residual = tmp_path / ".render_frame_map.json.tmp-residual"
    residual.write_text("keep", encoding="utf-8")
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def write(number: int) -> None:
        try:
            barrier.wait(timeout=5)
            source_fallback._write_json_atomic(destination, {"writer": number})
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(number,)) for number in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert json.loads(destination.read_text(encoding="utf-8"))["writer"] in {1, 2}
    assert residual.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".render_frame_map.json.*.tmp")) == []


def test_atomic_frame_map_cleanup_does_not_delete_replaced_nonowned_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "render_frame_map.json"
    attacker_file = tmp_path / "attacker-residual"
    attacker_file.write_text("do-not-delete", encoding="utf-8")
    original_same_file = source_fallback._same_regular_file
    swapped_path: Path | None = None

    def replace_before_identity_check(
        path: Path, expected: object,
    ) -> bool:
        nonlocal swapped_path
        if swapped_path is None:
            path.unlink()
            attacker_file.replace(path)
            swapped_path = path
        return original_same_file(path, expected)

    monkeypatch.setattr(
        source_fallback, "_same_regular_file", replace_before_identity_check
    )

    with pytest.raises(OSError, match="identity changed"):
        source_fallback._write_json_atomic(destination, {"writer": "safe"})

    assert swapped_path is not None
    assert swapped_path.read_text(encoding="utf-8") == "do-not-delete"
    assert not destination.exists()


def test_source_fallback_adapter_is_manifest_free_and_returns_validated_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    manifest = tmp_path / "render_manifest.json"
    manifest.write_text('{"revision": 7}', encoding="utf-8")
    before = manifest.read_bytes()
    output = inputs.attempt_directory / "rendered.mp4"
    output.write_bytes(b"rendered")
    frame_map = build_source_render_frame_map(
        _index(),
        clip_id=inputs.clip_id,
        source_start_pts=inputs.source_start_pts,
        source_end_pts_exclusive=inputs.source_end_pts_exclusive,
        source_time_base=inputs.source_time_base,
    )
    (inputs.attempt_directory / "render_frame_map.json").write_text(
        json.dumps(frame_map), encoding="utf-8"
    )
    probe = parse_ffprobe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "profile": "High",
                    "width": 320,
                    "height": 180,
                    "sample_aspect_ratio": "1:1",
                    "pix_fmt": "yuv420p",
                    "time_base": "1/1000",
                    "avg_frame_rate": "25/1",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                    "color_primaries": "bt709",
                }
            ],
            "frames": [
                {"media_type": "video", "stream_index": 0, "pts": "0", "pkt_duration": "50"},
                {"media_type": "video", "stream_index": 0, "pts": "50", "pkt_duration": "40"},
            ],
            "format": {"duration": "0.09"},
        }
    )
    monkeypatch.setattr(
        "cadscene.projects.source_fallback.probe_decoded_frame_index",
        lambda *_args, **_kwargs: _index(),
    )
    monkeypatch.setattr(
        "cadscene.projects.source_fallback.probe_media",
        lambda *_args, **_kwargs: probe,
    )
    adapter = SourceIntervalRenderAdapter(
        ffmpeg_executable="ffmpeg-custom",
        ffprobe_executable="ffprobe-custom",
    )

    service = object.__new__(ProjectService)
    service.source_interval_render_adapter = adapter
    plan = service.prepare_source_interval_render(inputs)
    result = plan.validate()

    assert adapter.name == "source_interval_fallback"
    assert adapter.version == "1"
    assert plan.commands[0][:3] == (
        sys.executable,
        "-m",
        "cadscene.cli.render_source_interval",
    )
    assert "--ffmpeg" in plan.commands[0] and "ffmpeg-custom" in plan.commands[0]
    assert plan.commands[0][plan.commands[0].index("--project-id") + 1] == "project-1"
    assert result.status == "success"
    assert result.outputs == {
        "video": str(output),
        "frame_map": str(inputs.attempt_directory / "render_frame_map.json"),
    }
    assert result.validation_proof["rendered_frame_count"] == 2
    assert manifest.read_bytes() == before
    assert not hasattr(adapter, "workflow")


@pytest.mark.parametrize(
    "kwargs",
    (
        {"preset": ""},
        {"preset": "not-an-x264-preset"},
        {"crf": True},
        {"crf": -1},
        {"crf": 52},
    ),
)
def test_source_interval_adapter_rejects_invalid_encoder_parameters_at_init(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        SourceIntervalRenderAdapter(**kwargs)


@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffprobe is required for real source fallback integration",
)
def test_real_nonzero_vfr_source_interval_preserves_half_open_frame_identity(
    tmp_path: Path,
) -> None:
    ffmpeg = resolve_ffmpeg_executable()
    source = (tmp_path / "nonzero-vfr.mp4").resolve()
    subprocess.run(
        (
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=25:duration=0.2",
            "-vf",
            "settb=1/1000,select='eq(n,0)+eq(n,1)+eq(n,3)+eq(n,4)',setpts=PTS+5000",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-enc_time_base",
            "1/1000",
            "-video_track_timescale",
            "1000",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-use_editlist",
            "0",
            str(source),
        ),
        check=True,
    )
    index = probe_decoded_frame_index(source, ffmpeg_executable=ffmpeg)
    assert index.source_start_pts != 0
    assert len({b.pts - a.pts for a, b in zip(index.frames, index.frames[1:])}) > 1
    spec = ProjectMediaSpec(
        width=64,
        height=48,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 1000),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=None,
    )
    boundary = index.frames[2].pts
    intervals = (
        (index.source_start_pts, boundary),
        (boundary, index.source_end_pts_exclusive),
    )
    mapped_ordinals: list[int] = []
    mapped_pts: list[int] = []
    for number, (start, end) in enumerate(intervals, start=1):
        attempt = (tmp_path / f"attempt-real-{number}").resolve()
        attempt.mkdir()
        inputs = SourceIntervalRenderInputs(
            project_id="project-real",
            clip_id=f"clip-real-{number}",
            source_video_path=source,
            source_start_pts=start,
            source_end_pts_exclusive=end,
            source_time_base=index.time_base,
            attempt_directory=attempt,
            project_media_spec=spec,
        )
        if number == 1:
            video, frame_map_path = render_source_interval(
                inputs, ffmpeg_executable=ffmpeg
            )
        else:
            subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "cadscene.cli.render_source_interval",
                    "--project-id",
                    inputs.project_id,
                    "--source-video",
                    str(source),
                    "--clip-id",
                    inputs.clip_id,
                    "--source-start-pts",
                    str(start),
                    "--source-end-pts-exclusive",
                    str(end),
                    "--source-time-base",
                    f"{index.time_base.numerator}/{index.time_base.denominator}",
                    "--project-media-spec-json",
                    json.dumps(spec.to_dict(), separators=(",", ":")),
                    "--attempt-dir",
                    str(attempt),
                    "--ffmpeg",
                    str(ffmpeg),
                ),
                check=True,
            )
            video = attempt / "rendered.mp4"
            frame_map_path = attempt / "render_frame_map.json"
        payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
        mapped_ordinals.extend(
            item["source_decoded_frame_ordinal"] for item in payload["frames"]
        )
        mapped_pts.extend(item["source_pts"] for item in payload["frames"])
        assert video.is_file()
        probed = probe_media(video)
        assert probed.video.time_base == spec.time_base
        assert probed.audio is None
        assert (
            probe_decoded_frame_index(video, ffmpeg_executable=ffmpeg).source_start_pts
            == 0
        )

    assert mapped_ordinals == [frame.ordinal for frame in index.frames]
    assert mapped_pts == [frame.pts for frame in index.frames]
    assert mapped_pts.count(boundary) == 1


@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffprobe is required for real source fallback integration",
)
@pytest.mark.parametrize(
    ("rotation", "expected_dimensions"),
    ((0, (64, 48)), (90, (48, 64))),
)
def test_real_default_edit_list_and_rotation_render_zero_start_without_reordering(
    tmp_path: Path,
    rotation: int,
    expected_dimensions: tuple[int, int],
) -> None:
    ffmpeg = resolve_ffmpeg_executable()
    ordinary = (tmp_path / "ordinary.mp4").resolve()
    subprocess.run(
        (
            str(ffmpeg),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=25:duration=0.2",
            "-frames:v",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(ordinary),
        ),
        check=True,
    )
    source = ordinary
    if rotation:
        source = (tmp_path / "rotated.mp4").resolve()
        subprocess.run(
            (
                str(ffmpeg),
                "-v",
                "error",
                "-display_rotation:v:0",
                str(rotation),
                "-i",
                str(ordinary),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                str(source),
            ),
            check=True,
        )
    index = probe_decoded_frame_index(source, ffmpeg_executable=ffmpeg)
    assert index.frames[0].pts == 0
    if rotation:
        raw_probe = json.loads(
            subprocess.run(
                (
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-of",
                    "json",
                    str(source),
                ),
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        side_data = raw_probe["streams"][0].get("side_data_list", [])
        assert any(abs(item.get("rotation", 0)) == 90 for item in side_data)
    attempt = (tmp_path / f"attempt-rotation-{rotation}").resolve()
    attempt.mkdir()
    width, height = expected_dimensions
    spec = ProjectMediaSpec(
        width=width,
        height=height,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=index.time_base,
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=None,
    )
    inputs = SourceIntervalRenderInputs(
        project_id="project-edit-list",
        clip_id=f"clip-rotation-{rotation}",
        source_video_path=source,
        source_start_pts=index.source_start_pts,
        source_end_pts_exclusive=index.source_end_pts_exclusive,
        source_time_base=index.time_base,
        attempt_directory=attempt,
        project_media_spec=spec,
    )

    video, frame_map_path = render_source_interval(
        inputs, ffmpeg_executable=ffmpeg
    )
    output = probe_media(video)
    frame_map = json.loads(frame_map_path.read_text(encoding="utf-8"))

    assert output.video.frame_pts[0] == 0
    assert all(pts >= 0 for pts in output.video.frame_pts)
    assert all(
        current > previous
        for previous, current in zip(
            output.video.frame_pts, output.video.frame_pts[1:]
        )
    )
    assert output.video.frame_count == len(frame_map["frames"]) == len(index.frames)
    assert output.video.display_orientation_baked is True
    assert (output.video.width, output.video.height) == expected_dimensions
