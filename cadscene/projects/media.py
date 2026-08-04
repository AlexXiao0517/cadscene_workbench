from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
import json
import math
from typing import Any, Mapping, Sequence

from cadscene.video_analysis.pts import DecodedFrameTimestamp


class InvalidMediaContract(ValueError):
    pass


class FrameMapMismatch(InvalidMediaContract):
    pass


class AudioVideoDurationMismatch(InvalidMediaContract):
    pass


@dataclass(frozen=True)
class ProjectMediaSpec:
    width: int
    height: int
    display_orientation_baked: bool
    sample_aspect_ratio: Fraction
    pixel_format: str
    codec_name: str
    profile: str
    time_base: Fraction
    color_range: str
    color_space: str
    color_transfer: str
    color_primaries: str
    nominal_frame_rate: Fraction | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.width, bool)
            or not isinstance(self.width, int)
            or isinstance(self.height, bool)
            or not isinstance(self.height, int)
            or self.width <= 0
            or self.height <= 0
        ):
            raise InvalidMediaContract("media dimensions must be positive")
        if self.display_orientation_baked is not True:
            raise InvalidMediaContract("project media orientation must be baked")
        if (
            not isinstance(self.sample_aspect_ratio, Fraction)
            or not isinstance(self.time_base, Fraction)
            or self.sample_aspect_ratio <= 0
            or self.time_base <= 0
        ):
            raise InvalidMediaContract("SAR and time base must be positive")
        if self.nominal_frame_rate is not None and (
            not isinstance(self.nominal_frame_rate, Fraction)
            or self.nominal_frame_rate <= 0
        ):
            raise InvalidMediaContract("nominal frame rate must be positive")
        for name in (
            "pixel_format",
            "codec_name",
            "profile",
            "color_range",
            "color_space",
            "color_transfer",
            "color_primaries",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
            ):
                raise InvalidMediaContract(f"{name} must be explicit")

    @property
    def color_metadata(self) -> dict[str, str]:
        return {
            "range": self.color_range,
            "space": self.color_space,
            "transfer": self.color_transfer,
            "primaries": self.color_primaries,
        }

    @property
    def baked_orientation(self) -> bool:
        return self.display_orientation_baked

    @property
    def sar(self) -> Fraction:
        return self.sample_aspect_ratio

    @property
    def pix_fmt(self) -> str:
        return self.pixel_format

    @property
    def codec(self) -> str:
        return self.codec_name

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "display_orientation_baked": self.display_orientation_baked,
            "sample_aspect_ratio": _fraction_dict(self.sample_aspect_ratio),
            "pixel_format": self.pixel_format,
            "codec_name": self.codec_name,
            "profile": self.profile,
            "time_base": _fraction_dict(self.time_base),
            "color_range": self.color_range,
            "color_space": self.color_space,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "nominal_frame_rate": (
                None
                if self.nominal_frame_rate is None
                else _fraction_dict(self.nominal_frame_rate)
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ProjectMediaSpec:
        frame_rate = payload.get("nominal_frame_rate")
        return cls(
            width=_integer(payload.get("width"), "width", minimum=1),
            height=_integer(payload.get("height"), "height", minimum=1),
            display_orientation_baked=payload.get("display_orientation_baked") is True,
            sample_aspect_ratio=_fraction_value(
                payload.get("sample_aspect_ratio"), "sample_aspect_ratio"
            ),
            pixel_format=_text(payload.get("pixel_format"), "pixel_format"),
            codec_name=_text(payload.get("codec_name"), "codec_name"),
            profile=_text(payload.get("profile"), "profile"),
            time_base=_fraction_value(payload.get("time_base"), "time_base"),
            color_range=_text(payload.get("color_range"), "color_range"),
            color_space=_text(payload.get("color_space"), "color_space"),
            color_transfer=_text(payload.get("color_transfer"), "color_transfer"),
            color_primaries=_text(payload.get("color_primaries"), "color_primaries"),
            nominal_frame_rate=(
                None
                if frame_rate is None
                else _fraction_value(frame_rate, "nominal_frame_rate")
            ),
        )


@dataclass(frozen=True)
class VideoMediaInfo:
    width: int
    height: int
    display_orientation_baked: bool
    sample_aspect_ratio: Fraction
    pixel_format: str
    codec_name: str
    profile: str
    time_base: Fraction
    color_range: str
    color_space: str
    color_transfer: str
    color_primaries: str
    nominal_frame_rate: Fraction | None
    frame_pts: tuple[int, ...]
    frame_duration_pts: tuple[int | None, ...]

    @property
    def frame_count(self) -> int:
        return len(self.frame_pts)


@dataclass(frozen=True)
class AudioMediaInfo:
    codec_name: str
    profile: str
    sample_format: str
    sample_rate: int
    channels: int
    channel_layout: str
    time_base: Fraction
    duration_sec: float | None


@dataclass(frozen=True)
class ProbedMedia:
    video: VideoMediaInfo
    audio: AudioMediaInfo | None
    format_duration_sec: float | None


@dataclass(frozen=True)
class RenderFrameMapEntry:
    output_frame_ordinal: int
    source_decoded_frame_ordinal: int
    source_pts: int


@dataclass(frozen=True)
class RenderValidationProof:
    rendered_frame_count: int
    output_pts: tuple[int, ...]
    source_pts: tuple[int, ...]
    source_time_base: Fraction


@dataclass(frozen=True)
class MediaCompatibility:
    compatible: bool
    differences: tuple[str, ...]


@dataclass(frozen=True)
class AudioVideoDurationValidation:
    audio_duration_sec: float
    video_duration_sec: float
    delta_sec: float
    tolerance_sec: float
    within_tolerance: bool


def parse_ffprobe(payload: str | Mapping[str, Any]) -> ProbedMedia:
    try:
        document = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        raise InvalidMediaContract(f"invalid ffprobe JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise InvalidMediaContract("ffprobe payload must be an object")
    streams = document.get("streams")
    frames = document.get("frames")
    if not isinstance(streams, list) or not isinstance(frames, list):
        raise InvalidMediaContract("ffprobe payload requires streams and frames")
    video_stream = next(
        (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise InvalidMediaContract("ffprobe payload has no video stream")
    video_index = _integer(video_stream.get("index", 0), "video.index", minimum=0)
    video_frames = [
        frame
        for frame in frames
        if isinstance(frame, Mapping)
        and frame.get("media_type", "video") == "video"
        and _optional_integer(frame.get("stream_index"), "frame.stream_index")
        in {None, video_index}
    ]
    frame_pts = tuple(_frame_pts(frame, ordinal) for ordinal, frame in enumerate(video_frames))
    frame_durations = tuple(
        _optional_integer(frame.get("pkt_duration"), "frame.pkt_duration")
        for frame in video_frames
    )
    rotation = _display_rotation(video_stream)
    video = VideoMediaInfo(
        width=_integer(video_stream.get("width"), "video.width", minimum=1),
        height=_integer(video_stream.get("height"), "video.height", minimum=1),
        display_orientation_baked=rotation % 360 == 0,
        sample_aspect_ratio=_fraction_value(
            video_stream.get("sample_aspect_ratio"),
            "video.sample_aspect_ratio",
            separator=":",
        ),
        pixel_format=_text(video_stream.get("pix_fmt"), "video.pix_fmt"),
        codec_name=_text(video_stream.get("codec_name"), "video.codec_name"),
        profile=_text(video_stream.get("profile"), "video.profile"),
        time_base=_fraction_value(video_stream.get("time_base"), "video.time_base"),
        color_range=_text(video_stream.get("color_range"), "video.color_range"),
        color_space=_text(video_stream.get("color_space"), "video.color_space"),
        color_transfer=_text(video_stream.get("color_transfer"), "video.color_transfer"),
        color_primaries=_text(video_stream.get("color_primaries"), "video.color_primaries"),
        nominal_frame_rate=_optional_fraction(
            video_stream.get("avg_frame_rate"), "video.avg_frame_rate"
        ),
        frame_pts=frame_pts,
        frame_duration_pts=frame_durations,
    )
    audio_stream = next(
        (item for item in streams if isinstance(item, Mapping) and item.get("codec_type") == "audio"),
        None,
    )
    audio = None if audio_stream is None else AudioMediaInfo(
        codec_name=_text(audio_stream.get("codec_name"), "audio.codec_name"),
        profile=_text(audio_stream.get("profile", "unknown"), "audio.profile"),
        sample_format=_text(audio_stream.get("sample_fmt"), "audio.sample_fmt"),
        sample_rate=_integer(audio_stream.get("sample_rate"), "audio.sample_rate", minimum=1),
        channels=_integer(audio_stream.get("channels"), "audio.channels", minimum=1),
        channel_layout=_text(audio_stream.get("channel_layout"), "audio.channel_layout"),
        time_base=_fraction_value(audio_stream.get("time_base"), "audio.time_base"),
        duration_sec=_optional_finite_float(audio_stream.get("duration"), "audio.duration"),
    )
    format_payload = document.get("format")
    format_duration = (
        _optional_finite_float(format_payload.get("duration"), "format.duration")
        if isinstance(format_payload, Mapping)
        else None
    )
    return ProbedMedia(video=video, audio=audio, format_duration_sec=format_duration)


def validate_video_pts(frame_pts: Sequence[int]) -> tuple[int, ...]:
    values = tuple(_integer(value, "video frame PTS") for value in frame_pts)
    if not values:
        raise InvalidMediaContract("rendered video contains no frames")
    if values[0] != 0:
        raise InvalidMediaContract("rendered video must start at zero PTS")
    if any(value < 0 for value in values):
        raise InvalidMediaContract("rendered video PTS must be non-negative")
    if any(current <= previous for previous, current in zip(values, values[1:])):
        raise InvalidMediaContract("rendered video PTS must be strictly monotonic")
    return values


def validate_render_frame_map(
    frame_map: Mapping[str, object],
    *,
    rendered_frame_count: int,
    expected_source_frames: Sequence[DecodedFrameTimestamp] | None = None,
    expected_source_time_base: Fraction | None = None,
) -> tuple[RenderFrameMapEntry, ...]:
    if not isinstance(frame_map, Mapping):
        raise FrameMapMismatch("render frame map must be an object")
    count = _integer(rendered_frame_count, "rendered_frame_count", minimum=0)
    raw_schema_version = frame_map.get("schema_version")
    try:
        schema_version = _integer(raw_schema_version, "schema_version", minimum=1)
    except InvalidMediaContract as exc:
        raise FrameMapMismatch("unsupported render frame map schema") from exc
    if type(raw_schema_version) is not int or schema_version != 1:
        raise FrameMapMismatch("unsupported render frame map schema")
    source_time_base = _fraction_value(
        frame_map.get("source_time_base"), "source_time_base"
    )
    if (
        expected_source_time_base is not None
        and source_time_base != expected_source_time_base
    ):
        raise FrameMapMismatch(
            "render frame map time base differs from authoritative source"
        )
    raw_entries = frame_map.get("frames", frame_map.get("entries"))
    if not isinstance(raw_entries, list):
        raise FrameMapMismatch("render frame map requires frames")
    entries: list[RenderFrameMapEntry] = []
    try:
        for output_ordinal, item in enumerate(raw_entries):
            if not isinstance(item, Mapping):
                raise FrameMapMismatch("render frame map entry must be an object")
            declared_output = item.get("output_frame_ordinal", item.get("output_ordinal"))
            source_ordinal = item.get("source_decoded_frame_ordinal", item.get("ordinal"))
            source_pts = item.get("source_pts", item.get("pts"))
            entry = RenderFrameMapEntry(
                output_frame_ordinal=_integer(
                    declared_output, "output_frame_ordinal", minimum=0
                ),
                source_decoded_frame_ordinal=_integer(
                    source_ordinal, "source_decoded_frame_ordinal", minimum=0
                ),
                source_pts=_integer(source_pts, "source_pts"),
            )
            if entry.output_frame_ordinal != output_ordinal:
                raise FrameMapMismatch("render frame map is not in output order")
            entries.append(entry)
    except InvalidMediaContract as exc:
        if isinstance(exc, FrameMapMismatch):
            raise
        raise FrameMapMismatch(str(exc)) from exc
    if len(entries) != count:
        raise FrameMapMismatch(
            "rendered frame count must equal render frame map entry count"
        )
    if any(
        current.source_decoded_frame_ordinal <= previous.source_decoded_frame_ordinal
        or current.source_pts <= previous.source_pts
        for previous, current in zip(entries, entries[1:])
    ):
        raise FrameMapMismatch("source decoded frames must be unique and ordered")
    if expected_source_frames is not None:
        expected = tuple((item.ordinal, item.pts) for item in expected_source_frames)
        actual = tuple(
            (item.source_decoded_frame_ordinal, item.source_pts) for item in entries
        )
        if actual != expected:
            raise FrameMapMismatch(
                "render frame map differs from authoritative source decoded frames"
            )
    return tuple(entries)


def validate_rendered_media(
    probe: ProbedMedia,
    frame_map: Mapping[str, object],
    *,
    expected_source_frames: Sequence[DecodedFrameTimestamp],
    expected_source_time_base: Fraction,
) -> RenderValidationProof:
    if (
        not isinstance(expected_source_frames, SequenceABC)
        or isinstance(expected_source_frames, (str, bytes, bytearray))
        or not expected_source_frames
        or any(
            not isinstance(frame, DecodedFrameTimestamp)
            for frame in expected_source_frames
        )
    ):
        raise InvalidMediaContract(
            "authoritative source frames must be a non-empty sequence"
        )
    if (
        not isinstance(expected_source_time_base, Fraction)
        or expected_source_time_base <= 0
    ):
        raise InvalidMediaContract(
            "authoritative source time base must be a positive Fraction"
        )
    if not probe.video.display_orientation_baked:
        raise InvalidMediaContract("rendered video display orientation is not baked")
    output_pts = validate_video_pts(probe.video.frame_pts)
    entries = validate_render_frame_map(
        frame_map,
        rendered_frame_count=probe.video.frame_count,
        expected_source_frames=expected_source_frames,
        expected_source_time_base=expected_source_time_base,
    )
    return RenderValidationProof(
        rendered_frame_count=len(output_pts),
        output_pts=output_pts,
        source_pts=tuple(item.source_pts for item in entries),
        source_time_base=_fraction_value(
            frame_map.get("source_time_base"), "source_time_base"
        ),
    )


def media_compatibility(
    actual: ProjectMediaSpec | VideoMediaInfo,
    expected: ProjectMediaSpec,
) -> MediaCompatibility:
    fields = (
        "width",
        "height",
        "display_orientation_baked",
        "sample_aspect_ratio",
        "pixel_format",
        "codec_name",
        "profile",
        "time_base",
        "nominal_frame_rate",
        "color_range",
        "color_space",
        "color_transfer",
        "color_primaries",
    )
    differences = tuple(
        field for field in fields if getattr(actual, field) != getattr(expected, field)
    )
    return MediaCompatibility(compatible=not differences, differences=differences)


def audio_video_duration_tolerance_sec(max_source_frame_duration_sec: float) -> float:
    duration = _finite_nonnegative(
        max_source_frame_duration_sec, "max_source_frame_duration_sec"
    )
    return float(max(Decimal("0.050"), Decimal(str(duration))))


def measure_audio_video_duration(
    *,
    audio_duration_sec: float,
    video_duration_sec: float,
    max_source_frame_duration_sec: float,
) -> AudioVideoDurationValidation:
    audio = _finite_nonnegative(audio_duration_sec, "audio_duration_sec")
    video = _finite_nonnegative(video_duration_sec, "video_duration_sec")
    tolerance = audio_video_duration_tolerance_sec(max_source_frame_duration_sec)
    delta_decimal = abs(Decimal(str(audio)) - Decimal(str(video)))
    tolerance_decimal = Decimal(str(tolerance))
    delta = float(delta_decimal)
    return AudioVideoDurationValidation(
        audio_duration_sec=audio,
        video_duration_sec=video,
        delta_sec=delta,
        tolerance_sec=tolerance,
        within_tolerance=delta_decimal <= tolerance_decimal,
    )


def validate_audio_video_duration(
    *,
    audio_duration_sec: float,
    video_duration_sec: float,
    max_source_frame_duration_sec: float,
) -> AudioVideoDurationValidation:
    result = measure_audio_video_duration(
        audio_duration_sec=audio_duration_sec,
        video_duration_sec=video_duration_sec,
        max_source_frame_duration_sec=max_source_frame_duration_sec,
    )
    if not result.within_tolerance:
        raise AudioVideoDurationMismatch(
            f"audio/video duration delta {result.delta_sec:.6f}s exceeds "
            f"{result.tolerance_sec:.6f}s tolerance"
        )
    return result


def _display_rotation(stream: Mapping[str, object]) -> int:
    rotations: list[int] = []
    tags = stream.get("tags")
    if isinstance(tags, Mapping) and tags.get("rotate") not in (None, ""):
        rotations.append(_integer(tags.get("rotate"), "video.tags.rotate") % 360)
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, Mapping) and item.get("rotation") is not None:
                rotations.append(_integer(item.get("rotation"), "video.rotation") % 360)
    if len(set(rotations)) > 1:
        raise InvalidMediaContract("conflicting display rotation metadata")
    return rotations[0] if rotations else 0


def _frame_pts(frame: Mapping[str, object], ordinal: int) -> int:
    value = frame.get("pts")
    if value in (None, "N/A"):
        value = frame.get("best_effort_timestamp")
    if value in (None, "N/A"):
        raise InvalidMediaContract(f"video frame {ordinal} has no decoded PTS")
    return _integer(value, f"video frame {ordinal} PTS")


def _fraction_value(
    value: object, field: str, *, separator: str = "/"
) -> Fraction:
    try:
        if isinstance(value, Fraction):
            result = value
        elif isinstance(value, Mapping):
            result = Fraction(
                _integer(value.get("numerator"), f"{field}.numerator"),
                _integer(value.get("denominator"), f"{field}.denominator"),
            )
        elif isinstance(value, str) and value.count(separator) == 1:
            numerator, denominator = value.split(separator, 1)
            result = Fraction(int(numerator), int(denominator))
        else:
            raise ValueError
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise InvalidMediaContract(f"{field} must be an exact rational") from exc
    if result <= 0:
        raise InvalidMediaContract(f"{field} must be positive")
    return result


def _optional_fraction(value: object, field: str) -> Fraction | None:
    if value in (None, "", "0/0", "N/A"):
        return None
    return _fraction_value(value, field)


def _fraction_dict(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise InvalidMediaContract(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidMediaContract(f"{field} must be an integer") from exc
    if isinstance(value, float) and value != result:
        raise InvalidMediaContract(f"{field} must be an integer")
    if isinstance(value, str) and str(result) != value.strip():
        raise InvalidMediaContract(f"{field} must be an integer")
    if minimum is not None and result < minimum:
        raise InvalidMediaContract(f"{field} must be at least {minimum}")
    return result


def _optional_integer(value: object, field: str) -> int | None:
    return None if value in (None, "N/A") else _integer(value, field)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidMediaContract(f"{field} must be explicit")
    return value.strip()


def _optional_finite_float(value: object, field: str) -> float | None:
    if value in (None, "N/A"):
        return None
    return _finite_nonnegative(value, field)


def _finite_nonnegative(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise InvalidMediaContract(f"{field} must be finite and non-negative")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidMediaContract(
            f"{field} must be finite and non-negative"
        ) from exc
    if not math.isfinite(result) or result < 0:
        raise InvalidMediaContract(f"{field} must be finite and non-negative")
    return result
