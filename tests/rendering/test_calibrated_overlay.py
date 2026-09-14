from __future__ import annotations

import numpy as np
import pytest
import json
from cadscene.cad.loader import CadBundle, RoadLine
from cadscene.rendering.calibrated_overlay import _drape, _flatten_cad

from cadscene.core.camera import CameraState
from cadscene.rendering.calibrated_overlay import (
    _draw_text_annotations,
    effective_camera_from_track,
    project_world_points,
    render_calibrated_frame,
)
from cadscene.rendering.calibration import CalibratedCameraModel


def test_shared_renderer_draws_chinese_annotation_pixels():
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    result = _draw_text_annotations(
        frame, state=CameraState(0, 0, 0, 0, 0, 0, 70),
        points_xyz=np.array([[0., 10., 0.]]), labels=("道路中心线",),
        heights_m=np.array([1.]), camera=CalibratedCameraModel(320, 180, 200, 160, 90, 0, 0),
        max_distance_m=350,
    )
    assert np.count_nonzero(result[50:130, 70:250]) > 100


def test_radial_projection_uses_true_xyz_and_distortion() -> None:
    model = CalibratedCameraModel(100, 80, 50.0, 50.0, 40.0, 0.1, 0.0)
    uv, depth = project_world_points(
        np.asarray([[1.0, 0.0, 5.0], [0.0, 0.0, 10.0]]),
        center=np.zeros(3),
        world_from_camera=np.eye(3),
        camera=model,
    )

    assert depth.tolist() == [5.0, 10.0]
    assert uv[0, 0] > 60.0
    assert uv[1].tolist() == [50.0, 40.0]


def test_frame_renderer_preserves_original_cad_colour() -> None:
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    model = CalibratedCameraModel(100, 80, 50.0, 50.0, 40.0, 0.0, 0.0)
    camera = CameraState(0, 0, 0, 0, 0, 0, 60)
    result = render_calibrated_frame(
        frame,
        camera,
        np.asarray([[-1.0, 5.0, 0.0]]),
        np.asarray([[1.0, 5.0, 0.0]]),
        np.asarray([[12, 34, 220]], dtype=np.uint8),
        np.asarray([2]),
        model,
        overlay_alpha=1.0,
        max_distance_m=20.0,
        fade_start_m=15.0,
    )

    assert np.any(np.all(result == [12, 34, 220], axis=2))


def test_default_render_keeps_same_line_opacity_at_near_and_far_depths():
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    model = CalibratedCameraModel(100, 80, 50., 50., 40., 0., 0.)
    state = CameraState(0, 0, 0, 0, 0, 0, 60)
    def render(depth):
        return render_calibrated_frame(frame, state,
            np.array([[-0.2 * depth, depth, 0.]]), np.array([[0.2 * depth, depth, 0.]]),
            np.array([[12, 34, 220]], dtype=np.uint8), np.array([2]), model)
    np.testing.assert_array_equal(render(80.), render(800.))


def test_formal_renderer_uses_consistent_track_fov_as_effective_intrinsic() -> None:
    source = CalibratedCameraModel(1920, 1080, 1500.0, 960.0, 540.0, -0.01, 0.04)
    cameras = {
        0: CameraState(0, 0, 0, 0, 0, 0, 90),
        1: CameraState(1, 0, 0, 0, 0, 0, 90),
    }

    adjusted, fov = effective_camera_from_track(source, cameras)

    assert fov == 90.0
    assert adjusted.focal_px == pytest.approx(960.0)
    assert adjusted.k1 == source.k1 and adjusted.k2 == source.k2


def test_formal_renderer_rejects_per_frame_fov_drift() -> None:
    source = CalibratedCameraModel(1920, 1080, 1500.0, 960.0, 540.0, 0.0, 0.0)
    cameras = {
        0: CameraState(0, 0, 0, 0, 0, 0, 72),
        1: CameraState(1, 0, 0, 0, 0, 0, 74),
    }

    with pytest.raises(ValueError, match="global horizontal FOV"):
        effective_camera_from_track(source, cameras)


def test_terrain_renderer_clips_uncovered_geometry_and_prefers_terrain_lines(tmp_path):
    controls = tmp_path / "controls.npz"
    np.savez(controls, points_xyz=[[0., 0., 999.]],
             segment_starts_xyz=[[0., 0., 120.]], segment_ends_xyz=[[20., 0., 120.]])
    starts, ends, keep = _drape(np.array([[0., 0.], [1000., 0.]]),
                              np.array([[10., 0.], [1010., 0.]]), controls, "terrain",
                              fallback_z=125.)
    assert keep.tolist() == [True, False]
    assert starts[0, 2] == ends[0, 2] == 120.


def test_flatten_uses_original_cad_bundle_and_densifies_long_lines(tmp_path):
    (tmp_path / "design.json").write_text(json.dumps({"layers": [{"entities": [
        {"world_points": [[0, 0], [1, 0]], "color": "#ff0000"}]}]}))
    line = RoadLine(np.array([[0., 0.], [100., 0.]]), color_bgr=(12, 34, 56))
    cad = CadBundle([line], [], [], (0, 0), (0, 0, 100, 0))
    starts, ends, colors = _flatten_cad(cad, cad_dir=tmp_path, origin_xy=(0, 0), cad_scale=1)
    assert len(starts) == 5
    assert np.max(np.linalg.norm(ends-starts, axis=1)) <= 20
    assert np.all(colors == (12, 34, 56))


def test_labels_at_same_screen_position_are_suppressed():
    args = dict(state=CameraState(0, 0, 0, 0, 0, 0, 70),
                camera=CalibratedCameraModel(320, 180, 200, 160, 90, 0, 0),
                max_distance_m=350)
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    single = _draw_text_annotations(frame, points_xyz=np.array([[0., 10., 0.]]),
                                   labels=("道路中心线",), heights_m=np.array([1.]), **args)
    duplicate = _draw_text_annotations(frame, points_xyz=np.array([[0., 10., 0.], [0., 20., 0.]]),
                                      labels=("道路中心线", "不应覆盖第一条"), heights_m=np.array([1., 2.]), **args)
    np.testing.assert_array_equal(single, duplicate)


def test_route_does_not_hide_distant_cad_before_densification(tmp_path):
    near = RoadLine(np.array([[0., 0.], [20., 0.]]))
    far = RoadLine(np.array([[1000., 0.], [1020., 0.]]))
    cad = CadBundle([near, far], [], [], (0, 0), (0, 0, 1020, 0))
    starts, ends, _ = _flatten_cad(cad, cad_dir=tmp_path, origin_xy=(0, 0), cad_scale=1,
                                  route_xy=np.array([[0., 0.], [10., 0.]]))
    assert starts.tolist() == [[0., 0.], [1000., 0.]]
    assert ends.tolist() == [[20., 0.], [1020., 0.]]


def test_distant_text_survives_without_distance_limit():
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    result = _draw_text_annotations(
        frame, state=CameraState(), points_xyz=np.array([[0., 800., 0.]]),
        labels=("远处道路",), heights_m=np.array([2.]),
        camera=CalibratedCameraModel(320, 180, 200, 160, 90, 0, 0), max_distance_m=None)
    assert np.count_nonzero(result) > 100


def test_visible_long_segment_is_not_discarded():
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    result = render_calibrated_frame(frame, CameraState(),
        np.array([[-20., 10., 0.]]), np.array([[20., 10., 0.]]),
        np.array([[0, 0, 255]], dtype=np.uint8), np.array([2]),
        CalibratedCameraModel(100, 80, 50, 50, 40, 0, 0))
    assert np.count_nonzero(result[40]) > 90


def test_render_text_prefers_raw_dxf_and_applies_terrain_height(tmp_path):
    from cadscene.rendering import calibrated_overlay as renderer
    raw = tmp_path / "raw_cad"
    raw.mkdir()
    (raw / "drawing.dxf").write_text("0\nTEXT\n10\n0\n20\n0\n40\n1\n1\nroad\n0\nEOF\n", encoding="utf-8")
    (tmp_path / "design.json").write_text('{"layers": []}')
    controls = tmp_path / "controls.npz"
    np.savez(controls, points_xyz=[], segment_starts_xyz=[[0., 0., 120.]],
             segment_ends_xyz=[[20., 0., 120.]])
    assert hasattr(renderer, "_prepare_text_annotations")
    points, labels, heights = renderer._prepare_text_annotations(
        tmp_path, origin_xy=(0, 0), cad_scale=1, route_xy=np.array([[0., 0.]]),
        controls_path=controls, mode="terrain", fallback_z=0)
    assert labels == ("road",)
    assert points[0, 2] == pytest.approx(120.3)
