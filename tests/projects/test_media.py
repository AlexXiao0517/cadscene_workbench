from __future__ import annotations

from fractions import Fraction
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cadscene.projects.media import (
    AudioVideoDurationMismatch,
    FrameMapMismatch,
    InvalidMediaContract,
    ProjectMediaSpec,
    audio_video_duration_tolerance_sec,
    media_compatibility,
    measure_audio_video_duration,
    parse_ffprobe,
    probe_media,
    validate_audio_video_duration,
    validate_render_frame_map,
    validate_rendered_media,
    validate_video_pts,
)
from cadscene.video_analysis.pts import DecodedFrameTimestamp


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for real media integration",
)
def test_real_ffprobe_accepts_micro_mp4_and_rejects_arbitrary_bytes(
    tmp_path: Path,
) -> None:
    encoders = subprocess.run(
        ("ffmpeg", "-hide_banner", "-encoders"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    encoder = next(
        (name for name in ("libx264", "libopenh264") if name in encoders), None
    )
    if encoder is None:
        pytest.skip("no software H.264 encoder is available")
    video = tmp_path / "micro.mp4"
    subprocess.run(
        (
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=16x16:r=25:d=0.08",
            "-frames:v",
            "2",
            "-vf",
            "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-c:v",
            encoder,
            "-pix_fmt",
            "yuv420p",
            str(video),
        ),
        check=True,
    )

    assert probe_media(video).video.frame_count == 2
    invalid = tmp_path / "invalid.mp4"
    invalid.write_bytes(b"not-an-mp4")
    with pytest.raises(InvalidMediaContract, match="ffprobe rejected"):
        probe_media(invalid)


def _probe_payload(*, pts: tuple[int, ...] = (0, 40, 80)) -> dict[str, object]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1",
                "pix_fmt": "yuv420p",
                "time_base": "1/1000",
                "avg_frame_rate": "25/1",
                "color_range": "tv",
                "color_space": "bt709",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_fmt": "fltp",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "time_base": "1/48000",
                "duration": "0.120",
            },
        ],
        "frames": [
            {
                "media_type": "video",
                "stream_index": 0,
                "pts": str(value),
                "pkt_duration": "40",
            }
            for value in pts
        ],
        "format": {"duration": "0.120"},
    }


def _standard_spec(**changes: object) -> ProjectMediaSpec:
    values: dict[str, object] = {
        "width": 1920,
        "height": 1080,
        "display_orientation_baked": True,
        "sample_aspect_ratio": Fraction(1, 1),
        "pixel_format": "yuv420p",
        "codec_name": "h264",
        "profile": "High",
        "time_base": Fraction(1, 1000),
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "nominal_frame_rate": Fraction(25, 1),
    }
    values.update(changes)
    return ProjectMediaSpec(**values)


def _frame_map() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "frames": [
            {
                "output_frame_ordinal": output,
                "source_decoded_frame_ordinal": source,
                "source_pts": pts,
            }
            for output, (source, pts) in enumerate(((10, 5000), (11, 5040), (12, 5080)))
        ],
    }


def test_project_media_spec_preserves_explicit_standard_video_contract() -> None:
    spec = _standard_spec()

    assert spec.width == 1920 and spec.height == 1080
    assert spec.display_orientation_baked is True
    assert spec.sample_aspect_ratio == Fraction(1, 1)
    assert spec.pixel_format == "yuv420p"
    assert spec.codec_name == "h264" and spec.profile == "High"
    assert spec.time_base == Fraction(1, 1000)
    assert spec.color_metadata == {
        "range": "tv",
        "space": "bt709",
        "transfer": "bt709",
        "primaries": "bt709",
    }
    assert ProjectMediaSpec.from_dict(spec.to_dict()) == spec


@pytest.mark.parametrize(
    "change",
    [
        {"width": 0},
        {"width": True},
        {"width": 1.5},
        {"display_orientation_baked": False},
        {"sample_aspect_ratio": Fraction(0, 1)},
        {"sample_aspect_ratio": 1.0},
        {"sample_aspect_ratio": float("nan")},
        {"time_base": Fraction(-1, 1000)},
        {"time_base": 0.001},
        {"nominal_frame_rate": 25.0},
        {"pixel_format": ""},
        {"codec_name": " h264"},
        {"profile": "High "},
        {"color_range": "   "},
        {"color_space": ""},
    ],
)
def test_project_media_spec_rejects_incomplete_or_unbaked_contract(
    change: dict[str, object],
) -> None:
    with pytest.raises(InvalidMediaContract):
        _standard_spec(**change)


def test_ffprobe_parser_selects_video_audio_frames_and_exact_rationals() -> None:
    probe = parse_ffprobe(json.dumps(_probe_payload()))

    assert probe.video.width == 1920
    assert probe.video.sample_aspect_ratio == Fraction(1, 1)
    assert probe.video.time_base == Fraction(1, 1000)
    assert probe.video.nominal_frame_rate == Fraction(25, 1)
    assert probe.video.display_orientation_baked is True
    assert probe.video.frame_pts == (0, 40, 80)
    assert probe.video.frame_duration_pts == (40, 40, 40)
    assert probe.audio is not None
    assert probe.audio.sample_rate == 48000
    assert probe.audio.channels == 2
    assert probe.audio.time_base == Fraction(1, 48000)


def test_ffprobe_parser_marks_rotation_metadata_as_not_baked() -> None:
    payload = _probe_payload()
    payload["streams"][0]["side_data_list"] = [{"rotation": 90}]

    probe = parse_ffprobe(payload)

    assert probe.video.display_orientation_baked is False


def test_ffprobe_parser_rejects_conflicting_tag_and_side_data_rotation() -> None:
    payload = _probe_payload()
    payload["streams"][0]["tags"] = {"rotate": "0"}
    payload["streams"][0]["side_data_list"] = [{"rotation": 90}]

    with pytest.raises(InvalidMediaContract, match="rotation"):
        parse_ffprobe(payload)


def test_ffprobe_parser_reads_all_consistent_rotation_metadata() -> None:
    payload = _probe_payload()
    payload["streams"][0]["tags"] = {"rotate": "90"}
    payload["streams"][0]["side_data_list"] = [
        {"side_data_type": "Display Matrix", "rotation": 90},
        {"side_data_type": "other"},
    ]

    assert parse_ffprobe(payload).video.display_orientation_baked is False


def test_ffprobe_parser_rejects_conflicting_side_data_rotations() -> None:
    payload = _probe_payload()
    payload["streams"][0]["side_data_list"] = [
        {"rotation": 90},
        {"rotation": 180},
    ]

    with pytest.raises(InvalidMediaContract, match="rotation"):
        parse_ffprobe(payload)


@pytest.mark.parametrize("pts", [(-1, 39, 79), (40, 80, 120), (0, 40, 40), (0, 80, 40)])
def test_rendered_video_pts_must_be_zero_start_nonnegative_and_strictly_monotonic(
    pts: tuple[int, ...],
) -> None:
    with pytest.raises(InvalidMediaContract):
        validate_video_pts(pts)


def test_rendered_video_pts_accept_irregular_monotonic_passthrough_timing() -> None:
    assert validate_video_pts((0, 33, 74, 110)) == (0, 33, 74, 110)


def test_rendered_frame_count_must_strictly_equal_render_frame_map() -> None:
    with pytest.raises(FrameMapMismatch):
        validate_render_frame_map(_frame_map(), rendered_frame_count=2)


def test_render_frame_map_preserves_output_order_and_authoritative_source_pts() -> None:
    source_frames = tuple(
        DecodedFrameTimestamp(ordinal, pts, 40, "pts")
        for ordinal, pts in ((10, 5000), (11, 5040), (12, 5080))
    )

    entries = validate_render_frame_map(
        _frame_map(),
        rendered_frame_count=3,
        expected_source_frames=source_frames,
    )

    assert [entry.output_frame_ordinal for entry in entries] == [0, 1, 2]
    assert [entry.source_decoded_frame_ordinal for entry in entries] == [10, 11, 12]
    assert [entry.source_pts for entry in entries] == [5000, 5040, 5080]


@pytest.mark.parametrize(
    "field,value",
    [
        ("output_frame_ordinal", 2),
        ("source_decoded_frame_ordinal", True),
        ("source_pts", 5000.5),
    ],
)
def test_render_frame_map_rejects_invalid_output_order_or_noninteger_source_identity(
    field: str, value: object,
) -> None:
    frame_map = _frame_map()
    frame_map["frames"][0][field] = value

    with pytest.raises(FrameMapMismatch):
        validate_render_frame_map(frame_map, rendered_frame_count=3)


def test_render_validation_combines_pts_count_and_frame_map_proof() -> None:
    probe = parse_ffprobe(_probe_payload())
    expected = tuple(
        DecodedFrameTimestamp(ordinal, pts, 40, "pts")
        for ordinal, pts in ((10, 5000), (11, 5040), (12, 5080))
    )

    proof = validate_rendered_media(
        probe,
        _frame_map(),
        expected_source_frames=expected,
        expected_source_time_base=Fraction(1, 1000),
    )

    assert proof.rendered_frame_count == 3
    assert proof.source_pts == (5000, 5040, 5080)
    assert proof.output_pts == (0, 40, 80)


@pytest.mark.parametrize(
    "mutation",
    [
        ("frame", 0, "source_decoded_frame_ordinal", 99),
        ("frame", 1, "source_pts", 5041),
        ("time_base", 1, 90000),
    ],
)
def test_render_validation_requires_exact_authoritative_source_identity(
    mutation: tuple[object, ...],
) -> None:
    probe = parse_ffprobe(_probe_payload())
    frame_map = deepcopy(_frame_map())
    expected = tuple(
        DecodedFrameTimestamp(ordinal, pts, 40, "pts")
        for ordinal, pts in ((10, 5000), (11, 5040), (12, 5080))
    )
    if mutation[0] == "frame":
        _, index, field, value = mutation
        frame_map["frames"][index][field] = value
    else:
        _, numerator, denominator = mutation
        frame_map["source_time_base"] = {
            "numerator": numerator,
            "denominator": denominator,
        }

    with pytest.raises(FrameMapMismatch):
        validate_rendered_media(
            probe,
            frame_map,
            expected_source_frames=expected,
            expected_source_time_base=Fraction(1, 1000),
        )


@pytest.mark.parametrize("invalid_frames", [None, 3.14, (), "frames"])
def test_render_validation_rejects_missing_nonsequence_or_empty_authority(
    invalid_frames: object,
) -> None:
    probe = parse_ffprobe(_probe_payload())

    with pytest.raises(InvalidMediaContract, match="source frames"):
        validate_rendered_media(
            probe,
            _frame_map(),
            expected_source_frames=invalid_frames,
            expected_source_time_base=Fraction(1, 1000),
        )


@pytest.mark.parametrize(
    "invalid_time_base", [None, 0.001, Fraction(0, 1)]
)
def test_render_validation_requires_positive_exact_fraction_source_time_base(
    invalid_time_base: object,
) -> None:
    probe = parse_ffprobe(_probe_payload())
    expected = tuple(
        DecodedFrameTimestamp(ordinal, pts, 40, "pts")
        for ordinal, pts in ((10, 5000), (11, 5040), (12, 5080))
    )

    with pytest.raises(InvalidMediaContract, match="positive Fraction"):
        validate_rendered_media(
            probe,
            _frame_map(),
            expected_source_frames=expected,
            expected_source_time_base=invalid_time_base,
        )


@pytest.mark.parametrize(
    "field,value",
    [("ordinal", 10.0), ("ordinal", True), ("pts", 5000.0), ("pts", True)],
)
def test_render_validation_requires_integer_authoritative_frame_identity(
    field: str, value: object,
) -> None:
    probe = parse_ffprobe(_probe_payload())
    values: dict[str, object] = {
        "ordinal": 10,
        "pts": 5000,
        "duration_pts": 40,
        "timestamp_source": "pts",
    }
    values[field] = value
    expected = (
        DecodedFrameTimestamp(**values),
        DecodedFrameTimestamp(11, 5040, 40, "pts"),
        DecodedFrameTimestamp(12, 5080, 40, "pts"),
    )

    with pytest.raises(InvalidMediaContract, match="integer identity"):
        validate_rendered_media(
            probe,
            _frame_map(),
            expected_source_frames=expected,
            expected_source_time_base=Fraction(1, 1000),
        )


@pytest.mark.parametrize("invalid_schema", [True, 1.0])
def test_render_frame_map_schema_version_is_strict_integer(
    invalid_schema: object,
) -> None:
    frame_map = _frame_map()
    frame_map["schema_version"] = invalid_schema

    with pytest.raises(FrameMapMismatch, match="schema"):
        validate_render_frame_map(frame_map, rendered_frame_count=3)


def test_media_compatibility_reports_each_standard_video_difference() -> None:
    actual = _standard_spec(width=1280, pixel_format="yuv444p", color_space="bt2020nc")

    result = media_compatibility(actual, _standard_spec())

    assert result.compatible is False
    assert set(result.differences) == {"width", "pixel_format", "color_space"}
    assert media_compatibility(_standard_spec(), _standard_spec()).compatible is True


def test_media_compatibility_allows_vfr_rate_when_project_nominal_rate_is_unset() -> None:
    actual = _standard_spec(nominal_frame_rate=Fraction(50, 3))
    expected = _standard_spec(nominal_frame_rate=None)

    result = media_compatibility(actual, expected)

    assert result.compatible is True
    assert result.differences == ()


def test_audio_video_tolerance_is_max_of_50ms_and_max_source_frame_duration() -> None:
    assert audio_video_duration_tolerance_sec(0.040) == pytest.approx(0.050)
    assert audio_video_duration_tolerance_sec(0.080) == pytest.approx(0.080)


def test_audio_video_duration_validation_records_delta_limit_and_rejects_excess() -> None:
    measured = measure_audio_video_duration(
        audio_duration_sec=10.04,
        video_duration_sec=10.0,
        max_source_frame_duration_sec=0.04,
    )
    assert measured.delta_sec == pytest.approx(0.04)
    assert measured.tolerance_sec == pytest.approx(0.05)
    assert measured.within_tolerance is True
    assert validate_audio_video_duration(
        audio_duration_sec=10.04,
        video_duration_sec=10.0,
        max_source_frame_duration_sec=0.04,
    ) == measured

    with pytest.raises(AudioVideoDurationMismatch):
        validate_audio_video_duration(
            audio_duration_sec=10.08,
            video_duration_sec=10.0,
            max_source_frame_duration_sec=0.04,
        )


def test_audio_video_duration_exact_decimal_boundary_is_inclusive() -> None:
    boundary = validate_audio_video_duration(
        audio_duration_sec=10.05,
        video_duration_sec=10.0,
        max_source_frame_duration_sec=0.04,
    )
    assert boundary.within_tolerance is True
    assert boundary.delta_sec == pytest.approx(0.05)

    with pytest.raises(AudioVideoDurationMismatch):
        validate_audio_video_duration(
            audio_duration_sec=10.0500001,
            video_duration_sec=10.0,
            max_source_frame_duration_sec=0.04,
        )


@pytest.mark.parametrize("invalid", [True, float("inf"), float("-inf")])
def test_integer_contract_inputs_map_bool_and_overflow_to_domain_error(
    invalid: object,
) -> None:
    payload = _probe_payload()
    payload["streams"][0]["width"] = invalid

    with pytest.raises(InvalidMediaContract, match="integer"):
        parse_ffprobe(payload)
