from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class Sim3:
    """SfM 世界到 CAD 世界的 7DoF 相似变换。"""

    scale: float
    rotation: np.ndarray
    translation: np.ndarray

    def __post_init__(self) -> None:
        object.__setattr__(self, "scale", float(self.scale))
        object.__setattr__(self, "rotation", np.asarray(self.rotation, dtype=np.float64).reshape(3, 3))
        object.__setattr__(self, "translation", np.asarray(self.translation, dtype=np.float64).reshape(3))
        if abs(self.scale) <= 1e-12:
            raise ValueError("Sim3 scale 不能为 0。")

    @classmethod
    def identity(cls) -> "Sim3":
        return cls(scale=1.0, rotation=np.eye(3, dtype=np.float64), translation=np.zeros(3, dtype=np.float64))

    @classmethod
    def from_dict(cls, data: dict) -> "Sim3":
        rotation = data.get("rotation", data.get("R", np.eye(3).tolist()))
        translation = data.get("translation", data.get("t", [0.0, 0.0, 0.0]))
        return cls(scale=float(data.get("scale", 1.0)), rotation=np.asarray(rotation), translation=np.asarray(translation))

    def to_dict(self) -> dict:
        return {
            "scale": float(self.scale),
            "rotation": self.rotation.tolist(),
            "translation": self.translation.tolist(),
        }

    def apply(self, points: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        arr = np.asarray(points, dtype=np.float64)
        return self.scale * (arr @ self.rotation.T) + self.translation

    def apply_point(self, point: Sequence[float] | np.ndarray) -> np.ndarray:
        return self.apply(np.asarray(point, dtype=np.float64).reshape(1, 3))[0]

    def inverse(self) -> "Sim3":
        inv_scale = 1.0 / self.scale
        inv_rotation = self.rotation.T
        inv_translation = -inv_scale * (inv_rotation @ self.translation)
        return Sim3(scale=inv_scale, rotation=inv_rotation, translation=inv_translation)

    def rotate_world_from_cam(self, r_cam_from_world: np.ndarray) -> np.ndarray:
        world_from_cam_sfm = np.asarray(r_cam_from_world, dtype=np.float64).T
        return self.rotation @ world_from_cam_sfm

