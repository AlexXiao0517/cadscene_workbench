from __future__ import annotations

from cadscene.annotations.render_overlay import AnnotationRenderEvent
from cadscene.cli.render_annotations import _write_overlay_images


def test_overlay_image_writer_reuses_one_transparent_png_for_eventless_frames(
    tmp_path,
) -> None:
    event = AnnotationRenderEvent(
        annotation_id="label-1",
        source_pts=101,
        start_sec=0.04,
        end_sec=0.08,
        x=120.0,
        y=50.0,
        anchor_x=40.0,
        anchor_y=60.0,
        text="正文",
        content={"title": "标题", "body": "正文"},
        panel={},
        leader={},
        style={},
    )
    bundle = {
        "video_width": 200,
        "video_height": 100,
        "source_frames": [
            {"source_pts": 100, "duration_pts": 1},
            {"source_pts": 101, "duration_pts": 1},
            {"source_pts": 102, "duration_pts": 1},
        ],
    }

    paths = _write_overlay_images(bundle, (event,), tmp_path)

    assert paths[0] == paths[2]
    assert paths[0].name == "transparent.png"
    assert len(list(tmp_path.glob("*.png"))) == 2
