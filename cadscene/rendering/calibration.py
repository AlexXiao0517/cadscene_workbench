from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class CalibratedCameraModel:
    width: int
    height: int
    focal_px: float
    cx_px: float
    cy_px: float
    k1: float
    k2: float

    def __post_init__(self) -> None:
        values = (self.focal_px, self.cx_px, self.cy_px, self.k1, self.k2)
        if self.width <= 0 or self.height <= 0 or self.focal_px <= 0:
            raise ValueError("calibrated camera dimensions and focal length must be positive")
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("calibrated camera values must be finite")

    @classmethod
    def from_json(cls, path: str | Path) -> "CalibratedCameraModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        width, height = payload["source_size"]
        return cls(
            int(width), int(height), float(payload["focal_px"]),
            float(payload["cx_px"]), float(payload["cy_px"]),
            float(payload.get("k1", 0.0)), float(payload.get("k2", 0.0)),
        )

    def scaled_to(self, width: int, height: int) -> "CalibratedCameraModel":
        if width <= 0 or height <= 0:
            raise ValueError("target calibration size must be positive")
        scale_x = width / self.width
        scale_y = height / self.height
        if not math.isclose(scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("target size must preserve the calibrated aspect ratio")
        return CalibratedCameraModel(
            width, height, self.focal_px * scale_x,
            self.cx_px * scale_x, self.cy_px * scale_y, self.k1, self.k2,
        )

    def with_horizontal_fov(self, fov_deg: float) -> "CalibratedCameraModel":
        value = float(fov_deg)
        if not math.isfinite(value) or not 1.0 < value < 179.0:
            raise ValueError("horizontal FOV must be inside (1, 179)")
        focal = self.width / (2.0 * math.tan(math.radians(value) / 2.0))
        return CalibratedCameraModel(
            self.width,
            self.height,
            focal,
            self.cx_px,
            self.cy_px,
            self.k1,
            self.k2,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "model": "RADIAL", "width": self.width, "height": self.height,
            "focal_px": self.focal_px, "cx_px": self.cx_px,
            "cy_px": self.cy_px, "k1": self.k1, "k2": self.k2,
        }


def output_dimensions(source_width: int, source_height: int, preset: str) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source dimensions must be positive")
    normalized = str(preset or "1080p").lower()
    if normalized == "source":
        return source_width, source_height
    if normalized == "4k":
        if source_width < 3840 or source_height < 2160:
            raise ValueError("4k output requires a 4K source")
        target_height = 2160
    elif normalized == "1080p":
        target_height = min(1080, source_height)
    elif normalized == "720p":
        target_height = min(720, source_height)
    else:
        raise ValueError(f"unsupported output_resolution: {preset}")
    target_width = max(2, int(round(source_width * target_height / source_height)))
    target_width -= target_width % 2
    target_height -= target_height % 2
    return target_width, target_height
