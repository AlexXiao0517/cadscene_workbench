from __future__ import annotations

from pathlib import Path

from cadscene.annotations.render_overlay import (
    build_annotation_events,
    build_drawtext_filter,
    build_overlay_concat_document,
    build_sendcmd_document,
    render_callout_overlay,
)


def test_render_events_use_exact_source_pts_and_skip_lost_video_frames() -> None:
    bundle = {
        "video_width": 200,
        "video_height": 100,
        "source_time_base": {"numerator": 1, "denominator": 25},
        "source_frames": [
            {"source_pts": 100, "duration_pts": 4},
            {"source_pts": 104, "duration_pts": 6},
            {"source_pts": 110, "duration_pts": 4},
        ],
        "annotations": [
            {
                "annotation_id": "video-label",
                "clip_id": "clip-1",
                "anchor_type": "video_track",
                "text": "vehicle",
                "style": {
                    "font_size_px": 28,
                    "text_color": "#FFFFFF",
                    "background_color": "#000000B3",
                    "border_color": "#FFFFFFCC",
                    "font_family": "sans-serif",
                    "font_weight": 600,
                },
                "source_pts_range": {
                    "start_pts": 100,
                    "end_pts_exclusive": 114,
                    "time_base": {"numerator": 1, "denominator": 25},
                    "semantics": "half_open",
                },
                "screen_offset": [10, -5],
                "visibility_policy": {"min_tracking_confidence": 0.5},
                "user_visible": True,
                "active_tracking_revision": "tracking-1",
            }
        ],
        "tracking_revisions": {
            "tracking-1": {
                "results": [
                    {
                        "source_pts": 100,
                        "anchor_xy": [40, 30],
                        "bbox": [20, 20, 40, 20],
                        "confidence": 0.9,
                        "visibility": True,
                        "tracking_status": "tracked",
                    },
                    {
                        "source_pts": 104,
                        "anchor_xy": None,
                        "bbox": None,
                        "confidence": 0.0,
                        "visibility": False,
                        "tracking_status": "lost",
                    },
                    {
                        "source_pts": 110,
                        "anchor_xy": [55, 31],
                        "bbox": [35, 21, 40, 20],
                        "confidence": 0.8,
                        "visibility": True,
                        "tracking_status": "tracked",
                    },
                ]
            }
        },
    }

    events = build_annotation_events(bundle, camera_rows=())

    assert [event.source_pts for event in events] == [100, 110]
    assert [(event.x, event.y) for event in events] == [(50.0, 25.0), (65.0, 26.0)]
    assert events[0].start_sec == 0.0
    assert events[0].end_sec == 0.16
    assert events[1].start_sec == 0.4
    assert events[1].end_sec == 0.56


def test_rgba_overlay_draws_card_polyline_anchor_title_and_multiline_body() -> None:
    bundle = {
        "video_width": 640,
        "video_height": 360,
        "source_time_base": {"numerator": 1, "denominator": 25},
        "source_frames": [{"source_pts": 100, "duration_pts": 1}],
        "annotations": [
            {
                "annotation_id": "cad-callout",
                "anchor_type": "cad_anchor",
                "text": "旧正文",
                "content": {"title": "K12+340", "body": "桥墩施工区域\n注意净空"},
                "anchor": {"cad_world_xyz": [0, 10, 1]},
                "style": {
                    "font_size_px": 24,
                    "text_color": "#FFFFFFFF",
                    "title_color": "#69D2FFFF",
                    "background_color": "#101820FF",
                    "background_opacity": 0.72,
                    "border_color": "#69D2FFFF",
                    "font_family": "sans-serif",
                    "font_weight": 600,
                },
                "panel": {"width_px": 260, "padding_px": 14, "safe_margin_px": 20},
                "leader": {"line_width_px": 2, "anchor_radius_px": 7, "elbow_length_px": 24},
                "source_pts_range": {"start_pts": 100, "end_pts_exclusive": 101},
                "screen_offset": [210, -100],
                "visibility_policy": {},
                "user_visible": True,
            }
        ],
        "tracking_revisions": {},
    }
    events = build_annotation_events(
        bundle,
        camera_rows=(
            {
                "frame_index": 0,
                "camera_x": 0,
                "camera_y": 0,
                "camera_z": 1,
                "yaw": 0,
                "pitch": 0,
                "roll": 0,
                "fov": 90,
            },
        ),
    )

    overlay = render_callout_overlay(events, width=640, height=360)
    alpha = overlay.getchannel("A")

    assert events[0].content == {"title": "K12+340", "body": "桥墩施工区域\n注意净空"}
    assert events[0].anchor_x == 320.0
    assert events[0].anchor_y == 180.0
    assert overlay.mode == "RGBA"
    assert alpha.getbbox() is not None
    assert alpha.getpixel((320, 180)) > 0
    assert alpha.getpixel((520, 80)) > 0


def test_overlay_concat_uses_authoritative_per_frame_durations(tmp_path: Path) -> None:
    document = build_overlay_concat_document(
        [tmp_path / "000.png", tmp_path / "001.png", tmp_path / "002.png"],
        source_frames=[
            {"source_pts": 100, "duration_pts": 4},
            {"source_pts": 104, "duration_pts": 6},
            {"source_pts": 110, "duration_pts": 5},
        ],
        time_base_numerator=1,
        time_base_denominator=100,
    )

    assert document.startswith("ffconcat version 1.0\n")
    assert document.count("file '") == 3
    assert "duration 0.040000000000" in document
    assert "duration 0.060000000000" in document
    assert "duration 0.050000000000" in document


def test_render_events_project_cad_through_shared_camera_math() -> None:
    bundle = {
        "video_width": 200,
        "video_height": 100,
        "source_time_base": {"numerator": 1, "denominator": 25},
        "source_frames": [{"source_pts": 100, "duration_pts": 1}],
        "annotations": [
            {
                "annotation_id": "cad-label",
                "clip_id": "clip-1",
                "anchor_type": "cad_anchor",
                "text": "K12+340",
                "anchor": {"cad_world_xyz": [0, 10, 1]},
                "style": {
                    "font_size_px": 28,
                    "text_color": "#FFFFFF",
                    "background_color": "#000000B3",
                    "border_color": "#FFFFFFCC",
                    "font_family": "sans-serif",
                    "font_weight": 600,
                },
                "source_pts_range": {
                    "start_pts": 100,
                    "end_pts_exclusive": 101,
                    "time_base": {"numerator": 1, "denominator": 25},
                    "semantics": "half_open",
                },
                "screen_offset": [10, -5],
                "visibility_policy": {},
                "user_visible": True,
                "active_tracking_revision": None,
            }
        ],
        "tracking_revisions": {},
    }
    camera_rows = (
        {
            "frame_index": 0,
            "camera_x": 0,
            "camera_y": 0,
            "camera_z": 1,
            "yaw": 0,
            "pitch": 0,
            "roll": 0,
            "fov": 90,
        },
    )

    events = build_annotation_events(bundle, camera_rows=camera_rows)

    assert len(events) == 1
    assert (events[0].x, events[0].y) == (110.0, 45.0)


def test_sendcmd_hides_at_exact_lost_boundary_and_reanchors_without_freeze() -> None:
    bundle = {
        "video_width": 200,
        "video_height": 100,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "source_frames": [
            {"source_pts": 1000, "duration_pts": 8},
            {"source_pts": 1008, "duration_pts": 9},
            {"source_pts": 1017, "duration_pts": 8},
        ],
        "annotations": [
            {
                "annotation_id": "target",
                "anchor_type": "video_track",
                "text": "target",
                "source_pts_range": {
                    "start_pts": 1000,
                    "end_pts_exclusive": 1025,
                },
                "screen_offset": [0, 0],
                "visibility_policy": {"min_tracking_confidence": 0.5},
                "user_visible": True,
                "active_tracking_revision": "tracking-1",
            }
        ],
        "tracking_revisions": {
            "tracking-1": {
                "results": [
                    {
                        "source_pts": 1000,
                        "anchor_xy": [20, 30],
                        "confidence": 0.9,
                        "visibility": True,
                    },
                    {
                        "source_pts": 1008,
                        "anchor_xy": None,
                        "confidence": 0.0,
                        "visibility": False,
                    },
                    {
                        "source_pts": 1017,
                        "anchor_xy": [70, 40],
                        "confidence": 0.8,
                        "visibility": True,
                    },
                ]
            }
        },
    }
    events = build_annotation_events(bundle, camera_rows=())

    commands, targets = build_sendcmd_document(events)

    assert targets == {"target": "Label0"}
    assert "0.000000000 [enter] drawtext@Label0 x 20.000000" in commands
    assert "0.008000000 [enter] drawtext@Label0 alpha 0" in commands
    assert "0.017000000 [enter] drawtext@Label0 x 70.000000" in commands
    assert "0.025000000 [enter] drawtext@Label0 alpha 0" in commands


def test_drawtext_filter_uses_one_runtime_updated_label_per_annotation() -> None:
    events = build_annotation_events(
        {
            "video_width": 200,
            "video_height": 100,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "source_frames": [{"source_pts": 100, "duration_pts": 1}],
            "annotations": [
                {
                    "annotation_id": "cad-label",
                    "anchor_type": "cad_anchor",
                    "text": "K12+340",
                    "anchor": {"cad_world_xyz": [0, 10, 1]},
                    "style": {
                        "font_size_px": 30,
                        "text_color": "#FFFFFFFF",
                        "background_color": "#000000B3",
                        "border_color": "#FFFFFFCC",
                        "font_family": "sans-serif",
                    },
                    "source_pts_range": {
                        "start_pts": 100,
                        "end_pts_exclusive": 101,
                    },
                    "screen_offset": [0, 0],
                    "visibility_policy": {},
                    "user_visible": True,
                }
            ],
            "tracking_revisions": {},
        },
        camera_rows=(
            {
                "frame_index": 0,
                "camera_x": 0,
                "camera_y": 0,
                "camera_z": 1,
                "yaw": 0,
                "pitch": 0,
                "roll": 0,
                "fov": 90,
            },
        ),
    )
    _, targets = build_sendcmd_document(events)

    graph = build_drawtext_filter(
        events,
        command_path=Path("C:/tmp/commands.txt"),
        text_paths={"cad-label": Path("C:/tmp/label.txt")},
        targets=targets,
        font_paths={"cad-label": Path("C:/Windows/Fonts/msyh.ttc")},
    )

    assert graph.count("drawtext@Label0") == 1
    assert "sendcmd=filename='C\\:/tmp/commands.txt'" in graph
    assert "textfile='C\\:/tmp/label.txt'" in graph
    assert "fontfile='C\\:/Windows/Fonts/msyh.ttc'" in graph
    assert "alpha=0" in graph
    assert "setpts" not in graph
