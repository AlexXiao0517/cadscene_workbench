from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Barrier, Thread

import pytest

from cadscene.projects.json_repositories import AtomicJsonRepository
from cadscene.projects.models import (
    ClipsManifest,
    JobsManifest,
    ProjectManifest,
    StateReference,
)
from cadscene.projects.repositories import (
    ManifestMutation,
    RevisionConflict,
    ordered_repositories,
    publish_manifests,
)


def _project_repository(path: Path) -> AtomicJsonRepository[ProjectManifest]:
    return AtomicJsonRepository(
        path,
        owner="project",
        decoder=ProjectManifest.from_dict,
    )


def _clips_repository(path: Path) -> AtomicJsonRepository[ClipsManifest]:
    return AtomicJsonRepository(
        path,
        owner="clips",
        decoder=ClipsManifest.from_dict,
    )


def _jobs_repository(path: Path) -> AtomicJsonRepository[JobsManifest]:
    return AtomicJsonRepository(
        path,
        owner="jobs",
        decoder=JobsManifest.from_dict,
    )


def _create_project(repository: AtomicJsonRepository[ProjectManifest]) -> None:
    repository.create(
        "p1",
        expected_revision=-1,
        value=ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
    )


def test_update_rejects_stale_expected_revision(tmp_path):
    repository = _project_repository(tmp_path / "project_manifest.json")
    _create_project(repository)

    first = repository.update(
        "p1",
        expected_revision=0,
        mutate=lambda value: replace(value, project_state="ready"),
    )
    with pytest.raises(RevisionConflict) as caught:
        repository.update(
            "p1",
            expected_revision=0,
            mutate=lambda value: replace(value, project_state="stale-write"),
        )

    assert first.revision == 1
    assert caught.value.expected_revision == 0
    assert caught.value.current_revision == 1
    assert repository.load("p1").project_state == "ready"


def test_atomic_write_uses_same_directory_flush_fsync_and_replace(tmp_path, monkeypatch):
    destination = tmp_path / "project_manifest.json"
    repository = _project_repository(destination)
    fsynced: list[int] = []
    replacements: list[tuple[Path, Path]] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    def recording_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    _create_project(repository)

    assert fsynced
    assert len(replacements) == 1
    temporary, target = replacements[0]
    assert temporary.parent == target.parent == tmp_path
    assert target == destination
    assert json.loads(destination.read_text(encoding="utf-8"))["revision"] == 0


def test_failed_replace_keeps_previous_manifest_and_removes_temp(tmp_path, monkeypatch):
    destination = tmp_path / "project_manifest.json"
    repository = _project_repository(destination)
    _create_project(repository)
    before = destination.read_bytes()

    def fail_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("simulated publication interruption")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="publication interruption"):
        repository.update(
            "p1",
            expected_revision=0,
            mutate=lambda value: replace(value, project_state="must-not-publish"),
        )

    assert destination.read_bytes() == before
    assert list(tmp_path.glob(".project_manifest.json-*.tmp")) == []


def test_repository_instances_share_a_process_lock_and_prevent_lost_updates(tmp_path):
    path = tmp_path / "project_manifest.json"
    first_repository = _project_repository(path)
    second_repository = _project_repository(path)
    _create_project(first_repository)
    start = Barrier(3)
    results: list[str] = []

    def update(repository: AtomicJsonRepository[ProjectManifest], state: str) -> None:
        start.wait()
        try:
            repository.update(
                "p1",
                expected_revision=0,
                mutate=lambda value: replace(value, project_state=state),
            )
            results.append("updated")
        except RevisionConflict:
            results.append("conflict")

    threads = [
        Thread(target=update, args=(first_repository, "first")),
        Thread(target=update, args=(second_repository, "second")),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert first_repository.process_lock is second_repository.process_lock
    assert sorted(results) == ["conflict", "updated"]
    assert first_repository.load("p1").revision == 1


def test_multi_manifest_lock_order_is_fixed_regardless_of_input_order(tmp_path):
    project = _project_repository(tmp_path / "project.json")
    clips = _clips_repository(tmp_path / "clips.json")
    jobs = AtomicJsonRepository(tmp_path / "jobs.json", owner="jobs", decoder=lambda x: x)
    render = AtomicJsonRepository(tmp_path / "render.json", owner="render", decoder=lambda x: x)

    ordered = ordered_repositories([render, jobs, project, clips])

    assert [repository.owner for repository in ordered] == [
        "project",
        "clips",
        "jobs",
        "render",
    ]


def test_cross_manifest_publication_stamps_one_unique_operation_id(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    clips_repository = _clips_repository(tmp_path / "clips.json")
    _create_project(project_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1",
            analysis_revision=None,
            updated_at="2026-08-03T00:00:00Z",
        ),
    )

    first = publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                0,
                lambda value: replace(value, project_state="analyzed"),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value: value,
            ),
        )
    )
    second = publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                1,
                lambda value: replace(value, project_state="ready"),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                1,
                lambda value: value,
            ),
        )
    )

    assert first.operation_id != second.operation_id
    assert {manifest.operation_id for manifest in first.manifests} == {
        first.operation_id
    }
    assert project_repository.load("p1").operation_id == second.operation_id
    assert clips_repository.load("p1").operation_id == second.operation_id


def test_cross_manifest_publication_stamps_new_states_and_references(tmp_path):
    clips_repository = _clips_repository(tmp_path / "clips.json")
    jobs_repository = _jobs_repository(tmp_path / "jobs.json")
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )
    jobs_repository.create(
        "p1",
        expected_revision=-1,
        value=JobsManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
    )

    result = publish_manifests(
        (
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value: replace(
                    value,
                    references=(
                        StateReference(
                            owner="jobs",
                            key="job:job-1",
                            operation_id="",
                        ),
                    ),
                ),
            ),
            ManifestMutation(
                jobs_repository,
                "p1",
                0,
                lambda value: replace(
                    value,
                    jobs=({"job_id": "job-1", "status": "queued"},),
                ),
            ),
        )
    )

    assert clips_repository.load("p1").references[0].operation_id == result.operation_id
    assert jobs_repository.load("p1").jobs[0]["operation_id"] == result.operation_id


def test_cross_manifest_publication_is_explicitly_not_a_transaction(tmp_path, monkeypatch):
    project_repository = _project_repository(tmp_path / "project.json")
    clips_repository = _clips_repository(tmp_path / "clips.json")
    _create_project(project_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1",
            analysis_revision=None,
            updated_at="2026-08-03T00:00:00Z",
        ),
    )

    def interrupt(_value: ClipsManifest) -> None:
        raise OSError("crash between manifest replacements")

    monkeypatch.setattr(clips_repository, "_atomic_write", interrupt)

    with pytest.raises(OSError, match="between manifest replacements"):
        publish_manifests(
            (
                ManifestMutation(project_repository, "p1", 0, lambda value: value),
                ManifestMutation(clips_repository, "p1", 0, lambda value: value),
            )
        )

    assert project_repository.load("p1").revision == 1
    assert project_repository.load("p1").operation_id is not None
    assert clips_repository.load("p1").revision == 0
    assert clips_repository.load("p1").operation_id is None


def test_cross_manifest_revision_conflicts_are_preflighted_before_any_replace(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    clips_repository = _clips_repository(tmp_path / "clips.json")
    _create_project(project_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )
    clips_repository.update("p1", expected_revision=0, mutate=lambda value: value)

    with pytest.raises(RevisionConflict):
        publish_manifests(
            (
                ManifestMutation(project_repository, "p1", 0, lambda value: value),
                ManifestMutation(clips_repository, "p1", 0, lambda value: value),
            )
        )

    assert project_repository.load("p1").revision == 0


def test_repository_rejects_a_manifest_serialized_for_another_owner(tmp_path):
    path = tmp_path / "project_manifest.json"
    wrong = ClipsManifest.new(
        "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
    )
    path.write_text(json.dumps(wrong.to_dict()), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest_owner"):
        _project_repository(path).load("p1")
