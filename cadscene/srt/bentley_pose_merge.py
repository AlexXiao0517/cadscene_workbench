"""Merge Bentley BlocksExchange camera orientations into DJI SRT telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import re
from typing import Sequence
from xml.etree import ElementTree

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from .parser import _parse_record_block


_FRAME_NAME = re.compile(r"(?:^|[/\\])frame_(\d+)\.[^/\\]+$", re.IGNORECASE)
_BLOCK_SEPARATOR = re.compile(r"((?:\r\n){2,}|\n{2,}|\r{2,})")
_EXISTING_CAMERA_ATTITUDE = re.compile(
    r"\[\s*(?:camera|gimbal|gb)[_ ]?(?:yaw|pitch|roll)\s*:",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BentleyPoseSample:
    frame_index: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    metadata_lon_lat_alt: tuple[float, float, float] | None


@dataclass(frozen=True)
class BentleySrtMergeResult:
    text: str
    report: dict[str, object]


def _required_finite_float(photo: ElementTree.Element, path: str) -> float:
    text = photo.findtext(path)
    try:
        value = float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bentley pose requires finite {path}") from exc
    if not isfinite(value):
        raise ValueError(f"Bentley pose requires finite {path}")
    return value


def _metadata_center(
    photo: ElementTree.Element,
) -> tuple[float, float, float] | None:
    center = photo.find("./Pose/Metadata/Center")
    if center is None:
        return None
    return tuple(
        _required_finite_float(photo, f"./Pose/Metadata/Center/{axis}")
        for axis in ("x", "y", "z")
    )  # type: ignore[return-value]


def _photo_sample(photo: ElementTree.Element) -> BentleyPoseSample:
    image_path = str(photo.findtext("./ImagePath") or "").strip()
    match = _FRAME_NAME.search(image_path)
    if match is None:
        raise ValueError(f"Bentley image path has no frame index: {image_path or '<empty>'}")
    frame_index = int(match.group(1))
    return BentleyPoseSample(
        frame_index=frame_index,
        yaw_deg=_required_finite_float(photo, "./Pose/Rotation/Yaw"),
        pitch_deg=_required_finite_float(photo, "./Pose/Rotation/Pitch"),
        roll_deg=_required_finite_float(photo, "./Pose/Rotation/Roll"),
        metadata_lon_lat_alt=_metadata_center(photo),
    )


def load_bentley_pose_samples(
    path: str | Path,
) -> tuple[BentleyPoseSample, ...]:
    """Load sorted camera poses whose image names encode video frame indices."""

    root = ElementTree.parse(Path(path)).getroot()
    samples = tuple(
        sorted(
            (_photo_sample(photo) for photo in root.findall(".//Photo")),
            key=lambda item: item.frame_index,
        )
    )
    if not samples:
        raise ValueError("Bentley XML contains no camera pose samples")
    if any(
        first.frame_index == second.frame_index
        for first, second in zip(samples, samples[1:])
    ):
        raise ValueError("Bentley pose samples require unique frame indices")
    return samples


def _split_blocks_losslessly(text: str) -> list[tuple[str, str]]:
    parts = _BLOCK_SEPARATOR.split(text)
    chunks: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        block = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if not block:
            if chunks and separator:
                previous_block, previous_separator = chunks[-1]
                chunks[-1] = (previous_block, previous_separator + separator)
            continue
        chunks.append((block, separator))
    if not chunks:
        raise ValueError("SRT contains no subtitle blocks")
    return chunks


def _validate_samples_against_srt(
    chunks: Sequence[tuple[str, str]],
    samples: Sequence[BentleyPoseSample],
) -> tuple[object, ...]:
    if not samples:
        raise ValueError("Bentley pose samples must not be empty")
    if int(samples[0].frame_index) != 0:
        raise ValueError("Bentley pose samples must start at frame index 0")
    if any(
        first.frame_index >= second.frame_index
        for first, second in zip(samples, samples[1:])
    ):
        raise ValueError("Bentley pose samples require increasing unique frame indices")
    if samples[-1].frame_index >= len(chunks):
        raise ValueError("Bentley pose frame index exceeds SRT block count")

    records: list[object] = []
    for block, _separator in chunks:
        if _EXISTING_CAMERA_ATTITUDE.search(block):
            raise ValueError("SRT already contains camera attitude tags")
        record = _parse_record_block(block)
        if record is None:
            raise ValueError("every SRT block must contain parseable telemetry")
        records.append(record)

    for sample in samples:
        metadata = sample.metadata_lon_lat_alt
        if metadata is None:
            continue
        record = records[sample.frame_index]
        longitude = getattr(record, "longitude", None)
        latitude = getattr(record, "latitude", None)
        altitude = getattr(record, "abs_alt", None)
        if longitude is None or latitude is None or altitude is None:
            raise ValueError("SRT lacks position required by Bentley metadata validation")
        expected = (float(longitude), float(latitude), float(altitude))
        tolerances = (1e-7, 1e-7, 1e-3)
        if any(
            abs(float(actual) - reference) > tolerance
            for actual, reference, tolerance in zip(metadata, expected, tolerances)
        ):
            raise ValueError(
                "Bentley metadata center disagrees with SRT position at "
                f"frame {sample.frame_index}"
            )
    return tuple(records)


def _sample_rotations(samples: Sequence[BentleyPoseSample]) -> Rotation:
    return Rotation.from_euler(
        "ZYX",
        [
            [sample.yaw_deg, sample.pitch_deg, sample.roll_deg]
            for sample in samples
        ],
        degrees=True,
    )


def _interpolated_rotations(
    block_count: int,
    samples: Sequence[BentleyPoseSample],
) -> Rotation:
    sample_rotations = _sample_rotations(samples)
    last_frame = samples[-1].frame_index
    interpolated_count = last_frame + 1
    if len(samples) == 1:
        matrices = np.repeat(
            sample_rotations.as_matrix()[0][None, :, :],
            interpolated_count,
            axis=0,
        )
        interpolated = Rotation.from_matrix(matrices)
    else:
        interpolation = Slerp(
            [float(sample.frame_index) for sample in samples],
            sample_rotations,
        )
        interpolated = interpolation(np.arange(interpolated_count, dtype=np.float64))
    if interpolated_count == block_count:
        return interpolated
    tail = np.repeat(
        sample_rotations.as_matrix()[-1][None, :, :],
        block_count - interpolated_count,
        axis=0,
    )
    return Rotation.from_matrix(
        np.concatenate((interpolated.as_matrix(), tail), axis=0)
    )


def _append_camera_tags(
    chunks: Sequence[tuple[str, str]],
    rotations: Rotation,
) -> str:
    eulers = rotations.as_euler("ZYX", degrees=True)
    output: list[str] = []
    for (block, separator), (yaw, pitch, roll) in zip(chunks, eulers):
        tags = (
            f" [camera_yaw: {yaw:.6f}]"
            f" [camera_pitch: {pitch:.6f}]"
            f" [camera_roll: {roll:.6f}]"
        )
        closing_font = block.lower().rfind("</font>")
        if closing_font >= 0:
            merged_block = block[:closing_font] + tags + block[closing_font:]
        else:
            trailing = re.search(r"((?:\r\n|\n|\r)*)$", block)
            insertion = trailing.start(1) if trailing is not None else len(block)
            merged_block = block[:insertion] + tags + block[insertion:]
        output.extend((merged_block, separator))
    return "".join(output)


def _build_report(
    block_count: int,
    samples: Sequence[BentleyPoseSample],
    rotations: Rotation,
    horizontal_fov_deg: float,
) -> dict[str, object]:
    matrices = rotations.as_matrix()
    adjacent = (
        Rotation.from_matrix(matrices[1:] @ np.swapaxes(matrices[:-1], 1, 2))
        if block_count > 1
        else None
    )
    max_step_deg = (
        0.0
        if adjacent is None
        else float(np.degrees(np.max(adjacent.magnitude())))
    )
    return {
        "schema_version": "1.0",
        "source_block_count": int(block_count),
        "xml_sample_count": int(len(samples)),
        "first_xml_frame": int(samples[0].frame_index),
        "last_xml_frame": int(samples[-1].frame_index),
        "interpolated_non_anchor_count": int(
            samples[-1].frame_index + 1 - len(samples)
        ),
        "held_tail_count": int(block_count - samples[-1].frame_index - 1),
        "camera_attitude_coverage": 1.0,
        "horizontal_fov_deg": float(horizontal_fov_deg),
        "position_policy": "original_srt_preserved",
        "orientation_source": "bentley_blocks_exchange_pose_rotation",
        "orientation_interpolation": "shortest_arc_quaternion_slerp",
        "max_adjacent_rotation_step_deg": max_step_deg,
    }


def merge_bentley_orientations_into_srt(
    srt_text: str,
    samples: Sequence[BentleyPoseSample],
    *,
    horizontal_fov_deg: float = 59.109,
) -> BentleySrtMergeResult:
    """Append interpolated Bentley camera attitudes without changing SRT telemetry."""

    if not isinstance(srt_text, str):
        raise TypeError("srt_text must be a string")
    if not isfinite(float(horizontal_fov_deg)) or not 1.0 < float(
        horizontal_fov_deg
    ) < 179.0:
        raise ValueError("horizontal_fov_deg must be finite and inside (1, 179)")
    chunks = _split_blocks_losslessly(srt_text)
    normalized_samples = tuple(samples)
    _validate_samples_against_srt(chunks, normalized_samples)
    rotations = _interpolated_rotations(len(chunks), normalized_samples)
    return BentleySrtMergeResult(
        text=_append_camera_tags(chunks, rotations),
        report=_build_report(
            len(chunks), normalized_samples, rotations, float(horizontal_fov_deg)
        ),
    )
