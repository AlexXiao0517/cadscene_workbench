"""Small, dependency-free data model for SRT telemetry records."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SrtRecord:
    """A single timed SRT metadata observation.

    Gimbal attitude represents camera attitude; aircraft attitude is retained
    separately and must never be treated as a full camera pose.
    """

    start_sec: float
    end_sec: float
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    rel_alt: float | None = None
    abs_alt: float | None = None
    gimbal_yaw: float | None = None
    gimbal_pitch: float | None = None
    gimbal_roll: float | None = None
    drone_yaw: float | None = None
    drone_pitch: float | None = None
    drone_roll: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
