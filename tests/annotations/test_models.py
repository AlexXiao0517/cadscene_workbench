from __future__ import annotations

from dataclasses import replace

import pytest

from cadscene.annotations.models import (
    Annotation,
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

