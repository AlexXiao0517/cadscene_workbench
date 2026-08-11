from __future__ import annotations

from pathlib import Path

import numpy as np

from cadscene.cad.loader import CadBundle, RoadLine, load_cad_bundle
from cadscene.cad.projection import project_point, style_alpha_for_distance
from cadscene.core.camera import CameraState
from cadscene.core.io import write_csv_utf8_sig
from cadscene.rendering.overlay import (
    RenderOverlayConfig,
    load_camera_path_csv,
    render_frame_overlay,
    render_overlay_video,
)


def _camera() -> CameraState:
    return CameraState(camera_x=0, camera_y=0, camera_z=1, yaw_deg=0, pitch_deg=0, roll_deg=0, fov_deg=70)


def _bundle(lines: list[RoadLine] | None = None) -> CadBundle:
    return CadBundle(centers=lines or [], edges=[], refs=[], origin_xy=(0, 0), bbox_xy=(0, 0, 0, 0))


def test_project_center_point_lands_near_image_center() -> None:
    uv = project_point(np.asarray([0.0, 10.0, 1.0]), _camera(), width=100, height=80)

    assert uv is not None
    assert abs(uv[0] - 50) < 1.0
    assert abs(uv[1] - 40) < 1.0


def test_projection_rejects_points_behind_camera() -> None:
    assert project_point(np.asarray([0.0, -10.0, 1.0]), _camera(), width=100, height=80) is None


def test_max_distance_filters_far_lines() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    cad = _bundle([RoadLine(points=np.asarray([[0.0, 100.0], [1.0, 100.0]]), kind="center")])

    out = render_frame_overlay(image, _camera(), cad, max_distance_m=20)

    assert np.array_equal(out, image)


def test_none_max_distance_keeps_far_lines_visible() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    cad = _bundle(
        [
            RoadLine(
                points=np.asarray([[0.0, 1000.0], [10.0, 1000.0]]),
                kind="center",
            )
        ]
    )

    out = render_frame_overlay(image, _camera(), cad, max_distance_m=None)

    assert int(out.sum()) > 0


def test_faded_overlay_alpha_decays_with_distance() -> None:
    near = style_alpha_for_distance(100, base_alpha=0.8, faded_overlay=True, fade_start_m=50, max_distance_m=300)
    far = style_alpha_for_distance(250, base_alpha=0.8, faded_overlay=True, fade_start_m=50, max_distance_m=300)

    assert 0 < far < near < 0.81


def test_empty_cad_does_not_crash_and_preserves_size() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)

    out = render_frame_overlay(image, _camera(), _bundle())

    assert out.shape == image.shape


def test_load_camera_path_csv(tmp_path: Path) -> None:
    path = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(path, [{"frame_index": 0, "camera_x": 1, "camera_y": 2, "camera_z": 3, "yaw": 4, "pitch": 5, "roll": 6, "fov": 70}])

    rows = load_camera_path_csv(path)

    assert rows[0][0] == 0
    assert rows[0][1].camera_x == 1
    assert rows[0][1].pitch_deg == 5


def test_render_frame_overlay_returns_same_dimensions() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    cad = _bundle([RoadLine(points=np.asarray([[0.0, 5.0], [1.0, 5.0]]), kind="center")])

    out = render_frame_overlay(image, _camera(), cad, overlay_linewidth=2, overlay_alpha=0.9)

    assert out.shape == image.shape
    assert int(out.sum()) > 0


def test_render_overlay_video_stats_fields_complete(tmp_path: Path) -> None:
    import cv2

    video = tmp_path / "in.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for _ in range(2):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    (cad_dir / "road_center.json").write_text('[{"points": [[0, 4], [1, 4]]}]', encoding="utf-8")
    path = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(path, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}])

    result = render_overlay_video(
        RenderOverlayConfig(
            video_path=video,
            cad_dir=cad_dir,
            sfm_camera_path=path,
            output_video=tmp_path / "out.mp4",
            max_distance_m=None,
        )
    )

    assert result.output_video.exists()
    assert result.stats["frame_count"] == 2
    assert result.stats["rendered_frame_count"] == 2
    assert result.stats["camera_path_frame_count"] == 1
    assert result.stats["cad_polyline_count"] == 1
    assert result.stats["max_distance_m"] is None
    assert "warnings" in result.stats


def test_design_json_fallback_converts_web_cad_world_to_cad_meters(tmp_path: Path) -> None:
    """旧 viewer 的 design.json 可直接作为渲染 CAD 输入。"""
    origin = (1000.0, 2000.0)
    (tmp_path / "design.json").write_text(
        '{"meta":{"coordinate_mode":"cad_world"},"layers":['
        '{"name":"road_center","color":"#ff0000","entities":[{"type":"polyline","world_points":[[1010,2020],[1030,2040]]}]},'
        '{"name":"road_edge","color":"#ffff00","entities":[{"type":"polyline","world_points":[[1000,2000],[1010,2000]]}]}'
        ']}',
        encoding="utf-8",
    )

    bundle = load_cad_bundle(tmp_path, origin_xy=origin, cad_scale=0.1)

    assert len(bundle.centers) == 1
    assert len(bundle.edges) == 1
    np.testing.assert_allclose(bundle.centers[0].points, [[1.0, 2.0], [3.0, 4.0]])
    np.testing.assert_allclose(bundle.edges[0].points, [[0.0, 0.0], [1.0, 0.0]])
    assert bundle.centers[0].color_bgr == (0, 0, 255)
    assert bundle.edges[0].color_bgr == (0, 255, 255)


def test_design_json_preserves_each_entity_color_before_layer_color(tmp_path: Path) -> None:
    """同一图层中的实体颜色必须独立保留，不能统一套用图层颜色。"""
    (tmp_path / "design.json").write_text(
        '{"meta":{"coordinate_mode":"cad_world"},"layers":['
        '{"name":"road_ref","color":"#ff0000","entities":['
        '{"type":"polyline","color":"#00ffff","world_points":[[0,0],[1,0]]},'
        '{"type":"polyline","color":"#ffffff","world_points":[[0,1],[1,1]]},'
        '{"type":"polyline","aci_color":3,"world_points":[[0,2],[1,2]]}'
        ']}'
        ']}',
        encoding="utf-8",
    )

    bundle = load_cad_bundle(tmp_path, origin_xy=(0.0, 0.0), cad_scale=1.0)

    assert [line.color_bgr for line in bundle.refs] == [
        (255, 255, 0),
        (255, 255, 255),
        (0, 255, 0),
    ]


def test_road_json_entities_preserve_legacy_aci_colors_and_convert_coordinates(tmp_path: Path) -> None:
    (tmp_path / "road_center.json").write_text(
        '{"entities":[{"points":[[1010,2020],[1030,2040]],"color":1}]}', encoding="utf-8"
    )
    (tmp_path / "road_edge.json").write_text(
        '{"entities":[{"points":[[1000,2000],[1010,2000]],"color":256}]}', encoding="utf-8"
    )
    (tmp_path / "road_ref.json").write_text(
        '{"entities":['
        '{"points":[[1000,2010],[1010,2010]],"color":4},'
        '{"points":[[1000,2020],[1010,2020]],"color":1}'
        ']}',
        encoding="utf-8",
    )

    bundle = load_cad_bundle(tmp_path, origin_xy=(1000.0, 2000.0), cad_scale=0.1)

    np.testing.assert_allclose(bundle.centers[0].points, [[1.0, 2.0], [3.0, 4.0]])
    assert bundle.centers[0].color_bgr == (0, 0, 255)
    assert bundle.edges[0].color_bgr == (0, 255, 255)
    assert [line.color_bgr for line in bundle.refs] == [(255, 255, 0), (0, 0, 255)]


def test_render_overlay_rejects_empty_cad_assets(tmp_path: Path) -> None:
    import cv2
    import pytest

    video = tmp_path / "in.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    path = tmp_path / "sfm_camera_path.csv"
    write_csv_utf8_sig(path, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}])

    with pytest.raises(ValueError, match="no renderable CAD polyline"):
        render_overlay_video(
            RenderOverlayConfig(
                video_path=video,
                cad_dir=tmp_path / "empty_cad",
                sfm_camera_path=path,
                output_video=tmp_path / "out.mp4",
            )
        )


def test_dense_polyline_is_drawn_in_batches(monkeypatch) -> None:
    import cv2

    line_calls = 0
    original_line = cv2.line

    def counted_line(*args, **kwargs):
        nonlocal line_calls
        line_calls += 1
        return original_line(*args, **kwargs)

    monkeypatch.setattr(cv2, "line", counted_line)
    points = np.column_stack([np.linspace(-10.0, 10.0, 2000), np.full(2000, 100.0)])
    cad = _bundle([RoadLine(points=points, kind="center")])

    output = render_frame_overlay(
        np.zeros((180, 320, 3), dtype=np.uint8),
        _camera(),
        cad,
        faded_overlay=True,
        max_distance_m=900,
        fade_start_m=250,
    )

    assert int(output.sum()) > 0
    assert line_calls <= 24


def test_render_overlay_video_prints_frame_progress(tmp_path: Path, capsys) -> None:
    import cv2

    video = tmp_path / "in.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for _ in range(2):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    (cad_dir / "road_center.json").write_text('[{"points": [[0, 4], [1, 4]]}]', encoding="utf-8")
    camera_path = tmp_path / "camera.csv"
    write_csv_utf8_sig(camera_path, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}])

    render_overlay_video(
        RenderOverlayConfig(
            video_path=video,
            cad_dir=cad_dir,
            sfm_camera_path=camera_path,
            output_video=tmp_path / "out.mp4",
        )
    )

    captured = capsys.readouterr().out
    assert "[render] frame 1/2" in captured
    assert "[render] frame 2/2" in captured


def test_render_overlay_video_reports_measured_frame_progress(tmp_path: Path) -> None:
    import cv2

    video = tmp_path / "in.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for _ in range(2):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    (cad_dir / "road_center.json").write_text(
        '[{"points": [[0, 4], [1, 4]]}]', encoding="utf-8"
    )
    camera_path = tmp_path / "camera.csv"
    write_csv_utf8_sig(
        camera_path,
        [
            {
                "frame_index": 0,
                "camera_x": 0,
                "camera_y": 0,
                "camera_z": 1,
                "yaw": 0,
                "pitch": 0,
                "roll": 0,
                "fov": 70,
            }
        ],
    )
    progress: list[tuple[str, str, float]] = []

    render_overlay_video(
        RenderOverlayConfig(
            video_path=video,
            cad_dir=cad_dir,
            sfm_camera_path=camera_path,
            output_video=tmp_path / "out.mp4",
        ),
        progress_callback=lambda stage, message, fraction: progress.append(
            (stage, message, fraction)
        ),
    )

    assert [item[2] for item in progress] == [0.5, 1.0]
    assert progress[-1][:2] == ("rendering_frames", "rendered frame 2/2")


def test_legacy_max_distance_and_fade_use_scaled_forward_depth() -> None:
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    near_line = _bundle([RoadLine(points=np.asarray([[-2.0, 10.0], [2.0, 10.0]]), kind="center")])
    faded_line = _bundle([RoadLine(points=np.asarray([[-2.0, 40.0], [2.0, 40.0]]), kind="center")])
    clipped_line = _bundle([RoadLine(points=np.asarray([[-2.0, 60.0], [2.0, 60.0]]), kind="center")])

    near = render_frame_overlay(image, _camera(), near_line, faded_overlay=True, max_distance_m=900, fade_start_m=250, cad_scale=0.06)
    faded = render_frame_overlay(image, _camera(), faded_line, faded_overlay=True, max_distance_m=900, fade_start_m=250, cad_scale=0.06)
    clipped = render_frame_overlay(image, _camera(), clipped_line, faded_overlay=True, max_distance_m=900, fade_start_m=250, cad_scale=0.06)

    assert int(near.sum()) > int(faded.sum()) > 0
    assert int(clipped.sum()) == 0


def test_sparse_legacy_polyline_is_densified_before_distance_clipping() -> None:
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    cad = _bundle(
        [RoadLine(points=np.asarray([[-20.0, -10.0], [20.0, 100.0]]), kind="center")]
    )

    output = render_frame_overlay(
        image,
        _camera(),
        cad,
        faded_overlay=True,
        max_distance_m=900,
        fade_start_m=250,
        cad_scale=0.06,
    )

    assert int(output.sum()) > 0


def test_sparse_polyline_without_fade_uses_densified_point_count() -> None:
    image = np.zeros((180, 320, 3), dtype=np.uint8)
    cad = _bundle(
        [RoadLine(points=np.asarray([[-20.0, -10.0], [20.0, 100.0]]), kind="center")]
    )

    output = render_frame_overlay(
        image,
        _camera(),
        cad,
        faded_overlay=False,
        max_distance_m=900,
        fade_start_m=250,
        cad_scale=0.06,
    )

    assert output.shape == image.shape
    assert int(output.sum()) > 0


def test_rendered_mp4_uses_browser_compatible_h264(tmp_path: Path) -> None:
    import cv2

    video = tmp_path / "in.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    (cad_dir / "road_center.json").write_text('[{"points": [[0, 4], [1, 4]]}]', encoding="utf-8")
    camera_path = tmp_path / "camera.csv"
    write_csv_utf8_sig(camera_path, [{"frame_index": 0, "camera_x": 0, "camera_y": 0, "camera_z": 1, "yaw": 0, "pitch": 0, "roll": 0, "fov": 70}])
    output = tmp_path / "out.mp4"

    render_overlay_video(RenderOverlayConfig(video_path=video, cad_dir=cad_dir, sfm_camera_path=camera_path, output_video=output))

    cap = cv2.VideoCapture(str(output))
    code = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join(chr((code >> (8 * index)) & 0xFF) for index in range(4)).lower()
    cap.release()
    assert codec in {"h264", "avc1"}
