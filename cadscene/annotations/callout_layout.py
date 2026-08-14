from __future__ import annotations

from dataclasses import dataclass
import math


Point = tuple[float, float]
Rect = tuple[float, float, float, float]


def _finite_pair(value: tuple[float, float], name: str) -> Point:
    if len(value) != 2:
        raise ValueError(f"{name} must contain x and y")
    pair = (float(value[0]), float(value[1]))
    if not all(math.isfinite(item) for item in pair):
        raise ValueError(f"{name} must contain finite values")
    return pair


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


@dataclass(frozen=True)
class CalloutLayout:
    anchor_xy: Point
    panel_rect: Rect
    leader_points: tuple[Point, Point, Point]

    def to_dict(self) -> dict[str, object]:
        return {
            "anchor_xy": list(self.anchor_xy),
            "panel_rect": list(self.panel_rect),
            "leader_points": [list(point) for point in self.leader_points],
        }


def layout_callout(
    *,
    anchor_xy: Point,
    screen_offset: Point,
    panel_size: Point,
    viewport_size: Point,
    safe_margin: float,
    elbow_length: float,
) -> CalloutLayout:
    """计算屏幕空间卡片、最近边缘连接点和折线引线。"""

    anchor_x, anchor_y = _finite_pair(anchor_xy, "anchor_xy")
    offset_x, offset_y = _finite_pair(screen_offset, "screen_offset")
    panel_width, panel_height = _finite_pair(panel_size, "panel_size")
    viewport_width, viewport_height = _finite_pair(viewport_size, "viewport_size")
    margin = float(safe_margin)
    elbow = float(elbow_length)
    if panel_width <= 0 or panel_height <= 0:
        raise ValueError("panel_size must be positive")
    if viewport_width <= 0 or viewport_height <= 0:
        raise ValueError("viewport_size must be positive")
    if margin < 0 or elbow < 0:
        raise ValueError("safe_margin and elbow_length must be non-negative")

    usable_width = max(1.0, viewport_width - 2.0 * margin)
    usable_height = max(1.0, viewport_height - 2.0 * margin)
    width = min(panel_width, usable_width)
    height = min(panel_height, usable_height)
    preferred_x = anchor_x + offset_x - width / 2.0
    preferred_y = anchor_y + offset_y - height / 2.0
    left = _clamp(preferred_x, margin, viewport_width - margin - width)
    top = _clamp(preferred_y, margin, viewport_height - margin - height)
    right = left + width
    bottom = top + height

    edge_x = _clamp(anchor_x, left, right)
    edge_y = _clamp(anchor_y, top, bottom)
    if anchor_x < left:
        elbow_point = (edge_x - elbow, edge_y)
    elif anchor_x > right:
        elbow_point = (edge_x + elbow, edge_y)
    elif anchor_y < top:
        elbow_point = (edge_x, edge_y - elbow)
    elif anchor_y > bottom:
        elbow_point = (edge_x, edge_y + elbow)
    else:
        distances = (
            (abs(anchor_x - left), "left"),
            (abs(right - anchor_x), "right"),
            (abs(anchor_y - top), "top"),
            (abs(bottom - anchor_y), "bottom"),
        )
        side = min(distances)[1]
        if side == "left":
            edge_x, elbow_point = left, (left - elbow, edge_y)
        elif side == "right":
            edge_x, elbow_point = right, (right + elbow, edge_y)
        elif side == "top":
            edge_y, elbow_point = top, (edge_x, top - elbow)
        else:
            edge_y, elbow_point = bottom, (edge_x, bottom + elbow)

    return CalloutLayout(
        anchor_xy=(anchor_x, anchor_y),
        panel_rect=(left, top, width, height),
        leader_points=((anchor_x, anchor_y), elbow_point, (edge_x, edge_y)),
    )
