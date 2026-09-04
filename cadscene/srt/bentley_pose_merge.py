"""Merge Bentley BlocksExchange camera orientations into DJI SRT telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import re
from xml.etree import ElementTree


_FRAME_NAME = re.compile(r"(?:^|[/\\])frame_(\d+)\.[^/\\]+$", re.IGNORECASE)


@dataclass(frozen=True)
class BentleyPoseSample:
    frame_index: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    metadata_lon_lat_alt: tuple[float, float, float] | None


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
