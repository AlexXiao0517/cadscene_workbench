from __future__ import annotations

from cadscene.annotations.cad_anchor import project_cad_anchor
from cadscene.annotations.models import Annotation, SourcePtsRange
from cadscene.core.camera import CameraState


def _annotation(
    world=(0.0, 10.0, 1.0), *, offset=(12.0, -8.0), visible=True
) -> Annotation:
    return Annotation.new(
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="K12+340",
        anchor={"cad_world_xyz": list(world)},
        source_pts_range=SourcePtsRange(100, 200, 1, 25),
        screen_offset=offset,
        user_visible=visible,
        created_at="now",
        operation_id="operation-1",
    )


def _camera(**changes) -> CameraState:
    values = {
        "camera_x": 0.0,
        "camera_y": 0.0,
        "camera_z": 1.0,
        "yaw_deg": 0.0,
        "pitch_deg": 0.0,
        "roll_deg": 0.0,
        "fov_deg": 90.0,
    }
    values.update(changes)
    return CameraState(**values)


def test_cad_anchor_reuses_camera_projection_and_applies_only_screen_offset() -> None:
    result = project_cad_anchor(
        _annotation(), _camera(), source_pts=120, width=200, height=100
    )

    assert result.visible is True
    assert result.anchor_xy == (100.0, 50.0)
    assert result.label_xy == (112.0, 42.0)
    assert result.depth_m == 10.0
    assert result.reason == "visible"


def test_cad_anchor_hides_behind_camera_outside_viewport_and_pts_range() -> None:
    behind = project_cad_anchor(
        _annotation(world=(0.0, -10.0, 1.0)),
        _camera(),
        source_pts=120,
        width=200,
        height=100,
    )
    outside = project_cad_anchor(
        _annotation(world=(30.0, 10.0, 1.0)),
        _camera(),
        source_pts=120,
        width=200,
        height=100,
    )
    before = project_cad_anchor(
        _annotation(), _camera(), source_pts=99, width=200, height=100
    )
    at_exclusive_end = project_cad_anchor(
        _annotation(), _camera(), source_pts=200, width=200, height=100
    )

    assert (behind.visible, behind.reason) == (False, "behind_camera")
    assert (outside.visible, outside.reason) == (False, "outside_viewport")
    assert (before.visible, before.reason) == (False, "outside_pts_range")
    assert (at_exclusive_end.visible, at_exclusive_end.reason) == (
        False,
        "outside_pts_range",
    )


def test_user_hidden_cad_anchor_never_projects() -> None:
    result = project_cad_anchor(
        _annotation(visible=False), _camera(), source_pts=120, width=200, height=100
    )

    assert result.visible is False
    assert result.reason == "user_hidden"
    assert result.anchor_xy is None
