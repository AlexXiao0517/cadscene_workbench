from __future__ import annotations

from dataclasses import replace

from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import (
    ClipsManifest,
    StateReference,
    activate_analysis_revision,
    register_analysis_revision,
)
from cadscene.projects.recovery import reconcile_project


def _created_repositories(tmp_path):
    repositories = project_repositories(tmp_path)
    repositories.create_project("p1", updated_at="2026-08-03T00:00:00Z")
    return repositories


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
    assert result.changed_owners == ("clips", "jobs", "render")
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
