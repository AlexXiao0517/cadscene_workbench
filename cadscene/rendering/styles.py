from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OverlayStyle:
    color_bgr: tuple[int, int, int]
    linewidth_scale: float = 1.0
    alpha_scale: float = 1.0


DEFAULT_STYLES: dict[str, OverlayStyle] = {
    "center": OverlayStyle(color_bgr=(40, 220, 255), linewidth_scale=1.0, alpha_scale=1.0),
    "edge": OverlayStyle(color_bgr=(80, 180, 80), linewidth_scale=0.8, alpha_scale=0.75),
    "ref": OverlayStyle(color_bgr=(255, 180, 60), linewidth_scale=0.65, alpha_scale=0.7),
    "unknown": OverlayStyle(color_bgr=(170, 170, 170), linewidth_scale=0.75, alpha_scale=0.65),
}


def style_for_kind(kind: str) -> OverlayStyle:
    return DEFAULT_STYLES.get(str(kind or "unknown").lower(), DEFAULT_STYLES["unknown"])
