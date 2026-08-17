from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock

from cadscene.annotations.models import AnnotationContent, SourcePtsRange
from cadscene.annotations.service import AnnotationService
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition


def _clip() -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "source_start_pts": 2250,
            "source_end_pts_exclusive": 3750,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "recommended_workflow": "sfm_only",
        },
        generated_display_name="Scene 1",
    )


def _service(tmp_path: Path):
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(_clip(),)
        ),
    )
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "render-1",
                    "clip_id": "clip-1",
                    "status": "success",
                    "operation_id": "render-operation",
                },
            ),
        ),
    )
    service = AnnotationService(
        repositories,
        now=lambda: "2026-08-13T02:00:00Z",
        identity=lambda: "identity-1",
        publication_lock=RLock(),
    )
    return service, repositories


def test_create_annotation_marks_only_corresponding_render_stale(tmp_path) -> None:
    service, repositories = _service(tmp_path)
    jobs_before = repositories.jobs.load("p1")

    result = service.create(
        "p1",
        expected_revision=0,
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="K12+340",
        anchor={"cad_world_xyz": [1.0, 2.0, 3.0]},
        source_pts_range=SourcePtsRange(2250, 3750, 1, 25),
    )

    annotations = repositories.annotations.load("p1")
    render = repositories.render.load("p1")
    assert result.annotation.annotation_id == "label-1"
    assert annotations.revision == 1
    assert annotations.annotations == (result.annotation,)
    assert render.clip_renders[0]["status"] == "stale_input"
    assert render.clip_renders[0]["stale_reason"] == "annotation_revision_changed"
    assert repositories.jobs.load("p1") == jobs_before


def test_edit_and_delete_use_manifest_and_item_expected_revisions(tmp_path) -> None:
    service, repositories = _service(tmp_path)
    created = service.create(
        "p1",
        expected_revision=0,
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="old",
        anchor={"cad_world_xyz": [1.0, 2.0, 3.0]},
        source_pts_range=SourcePtsRange(2250, 3750, 1, 25),
    )

    updated = service.update(
        "p1",
        "label-1",
        expected_revision=created.manifest_revision,
        expected_annotation_revision=0,
        changes={"text": "new", "screen_offset": [12.0, -8.0]},
    )
    deleted = service.delete(
        "p1",
        "label-1",
        expected_revision=updated.manifest_revision,
        expected_annotation_revision=1,
    )

    assert updated.annotation.text == "new"
    assert updated.annotation.screen_offset == (12.0, -8.0)
    assert updated.annotation.annotation_revision == 1
    assert deleted.annotation_id == "label-1"
    assert repositories.annotations.load("p1").annotations == ()


def test_editing_callout_content_and_offset_does_not_create_tracking_revision(
    tmp_path,
) -> None:
    service, repositories = _service(tmp_path)
    created = service.create(
        "p1",
        expected_revision=0,
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
        source_pts_range=SourcePtsRange(2250, 3750, 1, 25),
    )

    updated = service.update(
        "p1",
        "label-1",
        expected_revision=created.manifest_revision,
        expected_annotation_revision=0,
        changes={
            "content": {"title": "车辆", "body": "跟踪目标"},
            "screen_offset": [80.0, -40.0],
            "panel": {"width_px": 300},
            "leader": {"anchor_radius_px": 8},
        },
    )

    assert updated.annotation.content == AnnotationContent(title="车辆", body="跟踪目标")
    assert updated.annotation.text == "跟踪目标"
    assert updated.annotation.active_tracking_revision is None
    assert updated.annotation.screen_offset == (80.0, -40.0)
    assert not (tmp_path / "projects" / "p1" / "annotations").exists()
    assert repositories.render.load("p1").clip_renders[0]["status"] == "stale_input"


def test_identical_annotation_update_does_not_bump_revision_or_stale_render(
    tmp_path,
) -> None:
    service, repositories = _service(tmp_path)
    created = service.create(
        "p1",
        expected_revision=0,
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="K12+340",
        anchor={"cad_world_xyz": [1.0, 2.0, 3.0]},
        source_pts_range=SourcePtsRange(2250, 3750, 1, 25),
    )
    current_render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=current_render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "render-2",
                    "clip_id": "clip-1",
                    "status": "success",
                    "operation_id": "render-operation-2",
                },
            ),
        ),
    )
    annotations_before = repositories.annotations.load("p1")
    render_before = repositories.render.load("p1")

    result = service.update(
        "p1",
        "label-1",
        expected_revision=annotations_before.revision,
        expected_annotation_revision=created.annotation.annotation_revision,
        changes={
            "content": created.annotation.content.to_dict(),
            "panel": created.annotation.panel.to_dict(),
            "leader": created.annotation.leader.to_dict(),
            "style": created.annotation.style.to_dict(),
            "screen_offset": list(created.annotation.screen_offset),
            "source_pts_range": created.annotation.source_pts_range.to_dict(),
            "anchor": dict(created.annotation.anchor),
            "user_visible": created.annotation.user_visible,
        },
    )

    assert result.annotation == created.annotation
    assert result.manifest_revision == annotations_before.revision
    assert result.render_revision == render_before.revision
    assert repositories.annotations.load("p1") == annotations_before
    assert repositories.render.load("p1") == render_before
