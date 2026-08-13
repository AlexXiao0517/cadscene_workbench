from __future__ import annotations

from dataclasses import replace
import json

import pytest

from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import (
    ClipDefinition,
    ClipsManifest,
    StateReference,
    activate_analysis_revision,
    register_analysis_revision,
)
from cadscene.projects.recovery import reconcile_project
from cadscene.projects.repositories import ManifestMutation, publish_manifests


def _created_repositories(tmp_path):
    repositories = project_repositories(tmp_path)
    repositories.create_project("p1", updated_at="2026-08-03T00:00:00Z")
    return repositories


def test_recovery_preserves_clip_owned_saved_workbench_reference(tmp_path):
    repositories = _created_repositories(tmp_path)
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            register_analysis_revision(
                value, "analysis-1", operation_id="analysis-operation"
            ),
            active_analysis_revision="analysis-1",
            active_analysis_operation_id="analysis-operation",
        ),
    )
    reference = StateReference(
        "clips",
        "workbench:clip-1",
        "save-operation",
        {
            "status": "saved",
            "workbench_output_revision": "workbench-1",
            "workbench_output_fingerprint": "a" * 64,
        },
    )
    clip = ClipDefinition.from_analysis(
        {
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 10,
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "recommended_workflow": "sfm_only",
        },
        references=(reference,),
    )
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(clip,)
        ),
    )

    result = reconcile_project("p1", repositories=repositories)

    assert result.rolled_back_references == 0
    assert repositories.clips.load("p1").clips[0].references == (reference,)


def test_recovery_completes_reference_from_authoritative_owner_state(tmp_path):
    repositories = _created_repositories(tmp_path)
    jobs = repositories.jobs.load("p1")
    jobs = repositories.jobs.update(
        "p1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(
            value,
            jobs=(
                {
                    "job_id": "job-1",
                    "clip_id": "clip-1",
                    "status": "success",
                    "operation_id": "op-authoritative",
                },
            ),
        ),
    )
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            references=(
                StateReference(
                    owner="jobs",
                    key="job:job-1",
                    operation_id="op-interrupted",
                ),
            ),
        ),
    )

    result = reconcile_project("p1", repositories=repositories)

    repaired = repositories.clips.load("p1")
    assert result.completed_references == 1
    assert result.rolled_back_references == 0
    assert repaired.references[0].operation_id == "op-authoritative"
    assert repaired.operation_id == result.operation_id


def test_recovery_rolls_back_reference_when_owner_has_no_state(tmp_path):
    repositories = _created_repositories(tmp_path)
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            references=(
                StateReference(
                    owner="jobs",
                    key="job:missing",
                    operation_id="op-partial",
                ),
            ),
        ),
    )

    result = reconcile_project("p1", repositories=repositories)

    repaired = repositories.render.load("p1")
    assert result.completed_references == 0
    assert result.rolled_back_references == 1
    assert repaired.references == ()


def test_recovery_is_idempotent_when_references_match_owner(tmp_path):
    repositories = _created_repositories(tmp_path)

    first = reconcile_project("p1", repositories=repositories)
    revisions_after_first = tuple(
        repository.load("p1").revision for repository in repositories.in_lock_order()
    )
    second = reconcile_project("p1", repositories=repositories)

    assert first.operation_id is None
    assert second.operation_id is None
    assert revisions_after_first == tuple(
        repository.load("p1").revision for repository in repositories.in_lock_order()
    )


def test_recovery_completes_partial_project_creation(tmp_path, monkeypatch):
    repositories = project_repositories(tmp_path)
    original_write = repositories.clips._atomic_write

    def interrupt(_value):
        raise OSError("crash after project manifest creation")

    monkeypatch.setattr(repositories.clips, "_atomic_write", interrupt)
    try:
        repositories.create_project("p1", updated_at="2026-08-03T00:00:00Z")
    except OSError:
        pass
    monkeypatch.setattr(repositories.clips, "_atomic_write", original_write)

    result = reconcile_project("p1", repositories=repositories)

    manifests = tuple(repository.load("p1") for repository in repositories.in_lock_order())
    assert result.changed_owners == ("clips", "jobs", "render", "annotations")
    assert len({manifest.operation_id for manifest in manifests}) == 1


def test_recovery_rolls_back_partial_analysis_activation_to_owned_clips(tmp_path):
    repositories = _created_repositories(tmp_path)
    project = repositories.project.load("p1")
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: register_analysis_revision(
            value, "analysis-1", operation_id="op-analysis-1"
        ),
    )
    clips = repositories.clips.load("p1")
    clips = repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(value, analysis_revision="analysis-1"),
    )
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: register_analysis_revision(
            value, "analysis-2", operation_id="op-analysis-2"
        ),
    )
    candidate = replace(clips, analysis_revision="analysis-2")
    activated_project, _ = activate_analysis_revision(
        project, clips, candidate, operation_id="op-activate"
    )
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda _value: activated_project,
    )

    result = reconcile_project("p1", repositories=repositories)

    recovered = repositories.project.load("p1")
    assert result.changed_owners == ("project",)
    assert recovered.active_analysis_revision == "analysis-1"
    assert recovered.candidate_analysis_revision == "analysis-2"
    assert recovered.active_analysis_operation_id == "op-analysis-1"
    assert recovered.candidate_analysis_operation_id == "op-activate"


@pytest.mark.parametrize("crash_after_owner", ["project", "clips", "jobs", "render"])
def test_recovery_completes_every_partial_ordered_operation_prefix(
    tmp_path, monkeypatch, crash_after_owner
):
    repositories = _created_repositories(tmp_path)
    repository_order = repositories.in_lock_order()
    next_owner = {
        "project": "clips",
        "clips": "jobs",
        "jobs": "render",
        "render": None,
    }[crash_after_owner]
    if next_owner is not None:
        failing_repository = next(
            repository
            for repository in repository_order
            if repository.owner == next_owner
        )

        def interrupt(_path, _serialized):
            raise OSError(f"crash after {crash_after_owner}")

        monkeypatch.setattr(
            failing_repository, "_atomic_write_bytes", interrupt
        )

    mutations = (
        ManifestMutation(
            repositories.project,
            "p1",
            0,
            lambda value, _operation_id: replace(
                value, project_state="arbitrary-new-state"
            ),
        ),
        ManifestMutation(
            repositories.clips,
            "p1",
            0,
            lambda value, _operation_id: value,
        ),
        ManifestMutation(
            repositories.jobs,
            "p1",
            0,
            lambda value, _operation_id: replace(
                value, jobs=({"job_id": "job-arbitrary", "status": "queued"},)
            ),
        ),
        ManifestMutation(
            repositories.render,
            "p1",
            0,
            lambda value, _operation_id: replace(
                value,
                published_outputs=(
                    {"output_id": "output-arbitrary", "status": "planned"},
                ),
            ),
        ),
    )
    if next_owner is None:
        published = publish_manifests(mutations)
        expected_operation_id = published.operation_id
    else:
        with pytest.raises(OSError, match=f"after {crash_after_owner}"):
            publish_manifests(mutations)
        expected_operation_id = repositories.project.load("p1").operation_id
        monkeypatch.undo()

    first = reconcile_project("p1", repositories=repositories)
    revisions_after_first = tuple(
        repository.load("p1").revision for repository in repository_order
    )
    second = reconcile_project("p1", repositories=repositories)

    assert repositories.project.load("p1").project_state == "arbitrary-new-state"
    assert repositories.jobs.load("p1").jobs[0]["job_id"] == "job-arbitrary"
    assert (
        repositories.render.load("p1").published_outputs[0]["output_id"]
        == "output-arbitrary"
    )
    participants = repository_order[:4]
    assert {
        repository.load("p1").operation_id for repository in participants
    } == {expected_operation_id}
    assert second.changed_owners == ()
    assert tuple(
        repository.load("p1").revision for repository in repository_order
    ) == revisions_after_first


def test_recovery_preserves_reference_to_inactive_immutable_analysis(tmp_path):
    repositories = _created_repositories(tmp_path)
    project = repositories.project.load("p1")
    for revision, operation_id in (
        ("analysis-1", "op-analysis-1"),
        ("analysis-2", "op-analysis-2"),
        ("analysis-3", "op-analysis-3"),
    ):
        project = repositories.project.update(
            "p1",
            expected_revision=project.revision,
            mutate=lambda value, revision=revision, operation_id=operation_id: (
                register_analysis_revision(
                    value, revision, operation_id=operation_id
                )
            ),
        )
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            references=(
                StateReference(
                    owner="project",
                    key="analysis:analysis-2",
                    operation_id="stale-reference-operation",
                ),
            ),
        ),
    )

    result = reconcile_project("p1", repositories=repositories)

    reference = repositories.render.load("p1").references[0]
    assert result.completed_references == 1
    assert result.rolled_back_references == 0
    assert reference.operation_id == "op-analysis-2"


def test_analysis_reference_uses_immutable_creation_not_later_activation_operation(
    tmp_path,
):
    repositories = _created_repositories(tmp_path)
    project = repositories.project.load("p1")
    project = register_analysis_revision(
        project, "analysis-1", operation_id="op-analysis-1"
    )
    project = repositories.project.update(
        "p1", expected_revision=0, mutate=lambda _value: project
    )
    project = register_analysis_revision(
        project, "analysis-2", operation_id="op-analysis-2"
    )
    project = repositories.project.update(
        "p1", expected_revision=1, mutate=lambda _value: project
    )
    current = replace(
        repositories.clips.load("p1"), analysis_revision="analysis-1"
    )
    candidate = replace(current, analysis_revision="analysis-2")
    activated_project, activated_clips = activate_analysis_revision(
        project,
        current,
        candidate,
        operation_id="op-activation",
    )
    repositories.project.update(
        "p1", expected_revision=2, mutate=lambda _value: activated_project
    )
    repositories.clips.update(
        "p1", expected_revision=0, mutate=lambda _value: activated_clips
    )
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            references=(
                StateReference(
                    owner="project",
                    key="analysis:analysis-2",
                    operation_id="stale-reference-operation",
                ),
            ),
        ),
    )

    reconcile_project("p1", repositories=repositories)

    assert (
        repositories.render.load("p1").references[0].operation_id
        == "op-analysis-2"
    )


@pytest.mark.parametrize(
    "corruption",
    ["wrong_project", "wrong_operation", "participant_order"],
)
def test_recovery_validates_complete_intent_before_publishing_any_pending_owner(
    tmp_path, monkeypatch, corruption
):
    repositories = _created_repositories(tmp_path)

    def interrupt(_path, _serialized):
        raise OSError("crash after project")

    monkeypatch.setattr(repositories.clips, "_atomic_write_bytes", interrupt)
    with pytest.raises(OSError, match="after project"):
        publish_manifests(
            (
                ManifestMutation(
                    repositories.project,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, project_state="candidate"
                    ),
                ),
                ManifestMutation(
                    repositories.clips,
                    "p1",
                    0,
                    lambda value, _operation_id: value,
                ),
                ManifestMutation(
                    repositories.jobs,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value,
                        jobs=({"job_id": "job-1", "status": "queued"},),
                    ),
                ),
                ManifestMutation(
                    repositories.render,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value,
                        published_outputs=(
                            {"output_id": "output-1", "status": "planned"},
                        ),
                    ),
                ),
            )
        )
    monkeypatch.undo()
    path = repositories.project.path_for("p1")
    payload = json.loads(path.read_text(encoding="utf-8"))
    intent = payload["operation_intent"]
    if corruption == "wrong_project":
        intent["candidates"]["render"]["project_id"] = "other-project"
    elif corruption == "wrong_operation":
        intent["candidates"]["render"]["operation_id"] = "other-operation"
    else:
        intent["participants"] = ["project", "jobs", "clips", "render"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        reconcile_project("p1", repositories=repositories)

    assert repositories.clips.load("p1").revision == 0
    assert repositories.jobs.load("p1").revision == 0
    assert repositories.render.load("p1").revision == 0


def test_recovery_rejects_tampered_published_prefix_before_any_pending_write(
    tmp_path, monkeypatch
):
    repositories = _created_repositories(tmp_path)

    def interrupt(_path, _serialized):
        raise OSError("crash after project")

    monkeypatch.setattr(repositories.clips, "_atomic_write_bytes", interrupt)
    with pytest.raises(OSError, match="after project"):
        publish_manifests(
            (
                ManifestMutation(
                    repositories.project,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, project_state="intended"
                    ),
                ),
                ManifestMutation(
                    repositories.clips,
                    "p1",
                    0,
                    lambda value, _operation_id: value,
                ),
                ManifestMutation(
                    repositories.jobs,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, jobs=({"job_id": "job-1", "status": "queued"},)
                    ),
                ),
            )
        )
    monkeypatch.undo()

    project_path = repositories.project.path_for("p1")
    payload = json.loads(project_path.read_text(encoding="utf-8"))
    payload["project_state"] = "tampered"
    project_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="published manifest differs"):
        reconcile_project("p1", repositories=repositories)

    assert repositories.clips.load("p1").revision == 0
    assert repositories.jobs.load("p1").revision == 0


def test_render_ownership_uses_typed_keys_when_id_categories_collide(tmp_path):
    repositories = _created_repositories(tmp_path)
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "shared-id",
                    "status": "ready",
                    "operation_id": "op-clip-render",
                },
            ),
            merge_plans=(
                {
                    "merge_id": "shared-id",
                    "status": "ready",
                    "operation_id": "op-merge",
                },
            ),
            published_outputs=(
                {
                    "output_id": "shared-id",
                    "status": "ready",
                    "operation_id": "op-output",
                },
            ),
        ),
    )
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            references=(
                StateReference("render", "clip_render:shared-id", "stale"),
                StateReference("render", "merge:shared-id", "stale"),
                StateReference("render", "output:shared-id", "stale"),
            ),
        ),
    )

    result = reconcile_project("p1", repositories=repositories)

    references = repositories.clips.load("p1").references
    assert result.completed_references == 3
    assert result.rolled_back_references == 0
    assert [(reference.key, reference.operation_id) for reference in references] == [
        ("clip_render:shared-id", "op-clip-render"),
        ("merge:shared-id", "op-merge"),
        ("output:shared-id", "op-output"),
    ]
