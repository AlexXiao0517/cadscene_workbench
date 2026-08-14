from __future__ import annotations

from dataclasses import replace

import pytest

from cadscene.annotations.models import (
    Annotation,
    AnnotationContent,
    AnnotationLeader,
    AnnotationPanel,
    AnnotationStyle,
    AnnotationsManifest,
    SourcePtsRange,
    VisibilityPolicy,
)


def _pts_range() -> SourcePtsRange:
    return SourcePtsRange(
        start_pts=2250,
        end_pts_exclusive=3750,
        time_base_numerator=1,
        time_base_denominator=25,
    )


def test_cad_annotation_round_trips_all_user_and_provenance_state() -> None:
    annotation = Annotation.new(
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="K12+340",
        anchor={
            "cad_world_xyz": [12.5, 40.0, 3.2],
            "cad_entity_reference": {"handle": "A12"},
        },
        source_pts_range=_pts_range(),
        screen_offset=(18.0, -24.0),
        style=AnnotationStyle(font_size_px=30, text_color="#FFE066"),
        visibility_policy=VisibilityPolicy(),
        user_visible=True,
        created_at="2026-08-13T02:00:00Z",
        operation_id="operation-create",
    )

    restored = Annotation.from_dict(annotation.to_dict())

    assert restored == annotation
    assert restored.annotation_revision == 0
    assert restored.active_tracking_revision is None
    assert restored.created_operation_id == "operation-create"
    assert restored.updated_operation_id == "operation-create"


def test_engineering_callout_round_trips_title_body_panel_and_leader() -> None:
    annotation = Annotation.new(
        annotation_id="callout-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="",
        content=AnnotationContent(title="K12+340", body="桥墩施工区域\n注意净空"),
        panel=AnnotationPanel(width_px=320, padding_px=16, safe_margin_px=24),
        leader=AnnotationLeader(line_width_px=2, anchor_radius_px=7),
        anchor={"cad_world_xyz": [12.5, 40.0, 3.2]},
        source_pts_range=_pts_range(),
        style=AnnotationStyle(
            font_size_px=26,
            title_color="#69D2FFFF",
            background_opacity=0.72,
        ),
        created_at="2026-08-13T02:00:00Z",
        operation_id="operation-create",
    )

    payload = annotation.to_dict()
    restored = Annotation.from_dict(payload)

    assert restored == annotation
    assert payload["content"] == {
        "title": "K12+340",
        "body": "桥墩施工区域\n注意净空",
    }
    assert payload["panel"]["width_px"] == 320
    assert payload["leader"]["anchor_radius_px"] == 7
    assert payload["style"]["title_color"] == "#69D2FFFF"
    assert payload["style"]["background_opacity"] == 0.72
    assert restored.text == "桥墩施工区域\n注意净空"


def test_legacy_text_migrates_to_callout_body_without_losing_compatibility() -> None:
    legacy = Annotation.new(
        annotation_id="legacy-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="旧桩号 K12+340",
        anchor={"cad_world_xyz": [1.0, 2.0, 3.0]},
        source_pts_range=_pts_range(),
        created_at="now",
        operation_id="operation-create",
    ).to_dict()
    legacy.pop("content", None)
    legacy.pop("panel", None)
    legacy.pop("leader", None)

    restored = Annotation.from_dict(legacy)

    assert restored.content.title == ""
    assert restored.content.body == "旧桩号 K12+340"
    assert restored.to_dict()["text"] == "旧桩号 K12+340"


def test_video_annotation_requires_pts_bound_initialization() -> None:
    with pytest.raises(ValueError, match="initialization"):
        Annotation.new(
            annotation_id="label-1",
            clip_id="clip-1",
            anchor_type="video_track",
            text="vehicle",
            anchor={"bbox": [10.0, 20.0, 40.0, 30.0]},
            source_pts_range=_pts_range(),
            created_at="now",
            operation_id="operation-create",
        )


def test_annotations_manifest_rejects_duplicate_annotation_ids() -> None:
    annotation = Annotation.new(
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="video_track",
        text="vehicle",
        anchor={
            "initialization": {
                "source_pts": 2250,
                "bbox": [10.0, 20.0, 40.0, 30.0],
            }
        },
        source_pts_range=_pts_range(),
        created_at="now",
        operation_id="operation-create",
    )

    with pytest.raises(ValueError, match="annotation_id values must be unique"):
        AnnotationsManifest.new(
            "p1",
            updated_at="now",
            annotations=(annotation, replace(annotation, text="duplicate")),
        )


def test_pts_range_is_half_open_and_uses_exact_time_base() -> None:
    value = _pts_range()

    assert value.contains(2250)
    assert value.contains(3749)
    assert not value.contains(3750)
    assert value.to_dict() == {
        "start_pts": 2250,
        "end_pts_exclusive": 3750,
        "time_base": {"numerator": 1, "denominator": 25},
        "semantics": "half_open",
    }
