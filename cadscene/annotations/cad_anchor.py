from __future__ import annotations

from dataclasses import dataclass

from cadscene.cad.projection import project_point
from cadscene.core.camera import CameraState

from .models import Annotation


@dataclass(frozen=True)
class CadAnchorProjection:
    visible: bool
    reason: str
    anchor_xy: tuple[float, float] | None = None
    label_xy: tuple[float, float] | None = None
    depth_m: float | None = None


def _hidden(reason: str) -> CadAnchorProjection:
    return CadAnchorProjection(visible=False, reason=reason)


def project_cad_anchor(
    annotation: Annotation,
    camera: CameraState,
    *,
    source_pts: int,
    width: int,
    height: int,
) -> CadAnchorProjection:
    """Project one CAD annotation through the shared CAD camera math."""

    if annotation.anchor_type != "cad_anchor":
        raise ValueError("project_cad_anchor requires a cad_anchor annotation")
    if width <= 0 or height <= 0:
        raise ValueError("projection viewport must be positive")
    if not annotation.user_visible:
        return _hidden("user_hidden")
    if not annotation.source_pts_range.contains(source_pts):
        return _hidden("outside_pts_range")
    projected = project_point(
        annotation.anchor["cad_world_xyz"],
        camera,
        width=width,
        height=height,
    )
    if projected is None:
        return _hidden("behind_camera")
    anchor_xy = (projected.u, projected.v)
    if not (0.0 <= projected.u < float(width) and 0.0 <= projected.v < float(height)):
        return _hidden("outside_viewport")
    label_xy = (
        projected.u + annotation.screen_offset[0],
        projected.v + annotation.screen_offset[1],
    )
    return CadAnchorProjection(
        visible=True,
        reason="visible",
        anchor_xy=anchor_xy,
        label_xy=label_xy,
        depth_m=projected.depth_m,
    )
