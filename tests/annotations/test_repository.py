from __future__ import annotations

from dataclasses import replace

import pytest

from cadscene.annotations.models import Annotation, SourcePtsRange
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.repositories import RevisionConflict


def _annotation() -> Annotation:
    return Annotation.new(
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="cad_anchor",
        text="K12+340",
        anchor={"cad_world_xyz": [12.5, 40.0, 3.2]},
        source_pts_range=SourcePtsRange(2250, 3750, 1, 25),
        created_at="2026-08-13T02:00:00Z",
        operation_id="operation-create-label",
    )


def test_project_creation_publishes_independent_annotations_manifest(tmp_path) -> None:
    repositories = project_repositories(tmp_path)

    repositories.create_project("p1", updated_at="2026-08-13T02:00:00Z")

    annotations = repositories.annotations.load("p1")
    manifests = tuple(
        repository.load("p1") for repository in repositories.in_lock_order()
    )
    assert annotations.owner == "annotations"
    assert annotations.revision == 0
    assert annotations.annotations == ()
    assert repositories.annotations.path_for("p1").name == "annotations_manifest.json"
    assert len({manifest.operation_id for manifest in manifests}) == 1


def test_annotations_repository_rejects_stale_expected_revision(tmp_path) -> None:
    repositories = project_repositories(tmp_path)
    repositories.create_project("p1", updated_at="2026-08-13T02:00:00Z")

    updated = repositories.annotations.update(
        "p1",
        expected_revision=0,
        mutate=lambda manifest: replace(
            manifest,
            annotations=(_annotation(),),
        ),
    )

    with pytest.raises(RevisionConflict):
        repositories.annotations.update(
            "p1",
            expected_revision=0,
            mutate=lambda manifest: manifest,
        )
    assert updated.revision == 1
    assert repositories.annotations.load("p1").annotations == (_annotation(),)
