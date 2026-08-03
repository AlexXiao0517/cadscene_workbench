from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Barrier, Thread

import pytest

from cadscene.projects.json_repositories import AtomicJsonRepository, project_repositories
from cadscene.projects.models import (
    ClipDefinition,
    ClipsManifest,
    JobsManifest,
    ProjectManifest,
    RenderManifest,
    StateReference,
    activate_analysis_revision,
    register_analysis_revision,
)
from cadscene.projects.repositories import (
    ManifestRepository,
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


def _render_repository(path: Path) -> AtomicJsonRepository[RenderManifest]:
    return AtomicJsonRepository(
        path,
        owner="render",
        decoder=RenderManifest.from_dict,
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


def test_unchecked_prepared_publication_is_not_a_public_repository_operation(
    tmp_path,
):
    repository = _project_repository(tmp_path / "project_manifest.json")

    assert "publish_prepared" not in ManifestRepository.__dict__
    assert not hasattr(repository, "publish_prepared")


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
                lambda value, _operation_id: replace(
                    value, project_state="analyzed"
                ),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value, _operation_id: value,
            ),
        )
    )
    second = publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                1,
                lambda value, _operation_id: replace(value, project_state="ready"),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                1,
                lambda value, _operation_id: value,
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
                lambda value, _operation_id: replace(
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
                lambda value, _operation_id: replace(
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

    def interrupt(_path, _serialized) -> None:
        raise OSError("crash between manifest replacements")

    monkeypatch.setattr(clips_repository, "_atomic_write_bytes", interrupt)

    with pytest.raises(OSError, match="between manifest replacements"):
        publish_manifests(
            (
                ManifestMutation(
                    project_repository, "p1", 0, lambda value, _operation_id: value
                ),
                ManifestMutation(
                    clips_repository, "p1", 0, lambda value, _operation_id: value
                ),
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
                ManifestMutation(
                    project_repository, "p1", 0, lambda value, _operation_id: value
                ),
                ManifestMutation(
                    clips_repository, "p1", 0, lambda value, _operation_id: value
                ),
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


def test_cross_manifest_mutators_receive_shared_operation_for_analysis_activation(
    tmp_path,
):
    repositories = project_repositories(tmp_path)
    repositories.create_project("p1", updated_at="2026-08-03T00:00:00Z")
    project = repositories.project.update(
        "p1",
        expected_revision=0,
        mutate=lambda value: register_analysis_revision(
            value, "analysis-1", operation_id="op-analysis-1"
        ),
    )
    current_clip = ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "recommended_workflow": "sfm_only",
        },
        generated_display_name="Scene 01",
    )
    current_clips = repositories.clips.update(
        "p1",
        expected_revision=0,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(current_clip,)
        ),
    )
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: register_analysis_revision(
            value, "analysis-2", operation_id="op-analysis-2"
        ),
    )
    candidate_clip = ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "clip-1",
            "analysis_revision": "analysis-2",
            "recommended_workflow": "pure_rotation",
        },
        generated_display_name="Scene 01 refreshed",
    )
    candidate = replace(
        current_clips, analysis_revision="analysis-2", clips=(candidate_clip,)
    )

    def activate_clips_with_reference(value, operation_id):
        _, activated = activate_analysis_revision(
            project,
            value,
            candidate,
            operation_id=operation_id,
        )
        activated_clip = replace(
            activated.clips[0],
            references=(
                StateReference(
                    owner="project",
                    key="analysis:analysis-2",
                    operation_id=operation_id,
                ),
            ),
        )
        return replace(activated, clips=(activated_clip,))

    result = publish_manifests(
        (
            ManifestMutation(
                repositories.project,
                "p1",
                project.revision,
                lambda value, operation_id: activate_analysis_revision(
                    value,
                    current_clips,
                    candidate,
                    operation_id=operation_id,
                )[0],
            ),
            ManifestMutation(
                repositories.clips,
                "p1",
                current_clips.revision,
                activate_clips_with_reference,
            ),
        )
    )

    activated_project = repositories.project.load("p1")
    activated_clips = repositories.clips.load("p1")
    assert activated_project.operation_id == result.operation_id
    assert activated_project.active_analysis_operation_id == result.operation_id
    assert activated_clips.operation_id == result.operation_id
    assert activated_clips.clips[0].operation_id == result.operation_id
    assert (
        activated_clips.clips[0].references[0].operation_id
        == result.operation_id
    )


def test_later_mutator_exception_publishes_no_manifest(tmp_path):
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

    def reject(_value, _operation_id):
        raise ValueError("deterministic candidate rejection")

    with pytest.raises(ValueError, match="candidate rejection"):
        publish_manifests(
            (
                ManifestMutation(
                    project_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, project_state="must-not-publish"
                    ),
                ),
                ManifestMutation(clips_repository, "p1", 0, reject),
            )
        )

    assert project_repository.load("p1").revision == 0
    assert clips_repository.load("p1").revision == 0


def test_later_serialization_error_publishes_no_manifest(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    render_repository = _render_repository(tmp_path / "render.json")
    _create_project(project_repository)
    render_repository.create(
        "p1",
        expected_revision=-1,
        value=RenderManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
    )

    with pytest.raises(TypeError):
        publish_manifests(
            (
                ManifestMutation(
                    project_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, project_state="must-not-publish"
                    ),
                ),
                ManifestMutation(
                    render_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value,
                        published_outputs=(
                            {"output_id": "bad", "not_json": {"a-set"}},
                        ),
                    ),
                ),
            )
        )

    assert project_repository.load("p1").revision == 0
    assert render_repository.load("p1").revision == 0


def test_changed_nested_states_are_restamped_but_unchanged_history_is_preserved(
    tmp_path,
):
    clips_repository = _clips_repository(tmp_path / "clips.json")
    jobs_repository = _jobs_repository(tmp_path / "jobs.json")
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            ClipsManifest.new(
                "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
            ),
            references=(
                StateReference("jobs", "job:changed", "op-old", {"status": "queued"}),
                StateReference("jobs", "job:stable", "op-old", {"status": "success"}),
            ),
        ),
    )
    jobs_repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            JobsManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
            jobs=(
                {"job_id": "changed", "status": "queued", "operation_id": "op-old"},
                {"job_id": "stable", "status": "success", "operation_id": "op-old"},
            ),
        ),
    )

    result = publish_manifests(
        (
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value, _operation_id: replace(
                    value,
                    references=(
                        replace(value.references[0], value={"status": "running"}),
                        value.references[1],
                    ),
                ),
            ),
            ManifestMutation(
                jobs_repository,
                "p1",
                0,
                lambda value, _operation_id: replace(
                    value,
                    jobs=(
                        {**value.jobs[0], "status": "running"},
                        value.jobs[1],
                    ),
                ),
            ),
        )
    )

    references = clips_repository.load("p1").references
    jobs = jobs_repository.load("p1").jobs
    assert references[0].operation_id == result.operation_id
    assert jobs[0]["operation_id"] == result.operation_id
    assert references[1].operation_id == "op-old"
    assert jobs[1]["operation_id"] == "op-old"


def test_prevalidated_bytes_are_not_serialized_again_during_publication(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    calls = 0

    def stateful_encoder(value: ClipsManifest):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("encoder called after complete prevalidation")
        return value.to_dict()

    clips_repository = AtomicJsonRepository(
        tmp_path / "clips.json",
        owner="clips",
        decoder=ClipsManifest.from_dict,
        encoder=stateful_encoder,
    )
    _create_project(project_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )
    calls = 0

    publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                0,
                lambda value, _operation_id: replace(
                    value, project_state="published"
                ),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value, _operation_id: value,
            ),
        )
    )

    assert calls == 1
    assert project_repository.load("p1").revision == 1
    assert clips_repository.load("p1").revision == 1
    published_payload = json.loads(
        clips_repository.path_for("p1").read_text(encoding="utf-8")
    )
    intended_payload = published_payload["operation_intent"]["candidates"]["clips"]
    published_without_intent = {
        **published_payload,
        "operation_intent": None,
    }
    assert published_without_intent == intended_payload


def test_lossy_encoder_is_rejected_before_cross_manifest_publication(tmp_path):
    project_path = tmp_path / "project.json"
    creating_repository = _project_repository(project_path)
    clips_repository = _clips_repository(tmp_path / "clips.json")
    _create_project(creating_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )

    def lossy_encoder(value: ProjectManifest):
        payload = value.to_dict()
        payload.pop("project_state")
        return payload

    lossy_repository = AtomicJsonRepository(
        project_path,
        owner="project",
        decoder=ProjectManifest.from_dict,
        encoder=lossy_encoder,
    )

    with pytest.raises(ValueError, match="round-trip semantically"):
        publish_manifests(
            (
                ManifestMutation(
                    lossy_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: replace(
                        value, project_state="must-not-be-lost"
                    ),
                ),
                ManifestMutation(
                    clips_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: value,
                ),
            )
        )

    assert creating_repository.load("p1").revision == 0
    assert clips_repository.load("p1").revision == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "delete",
        "reorder",
        "rebind",
        "in_place_rebind",
        "wrong_new_operation",
    ],
)
def test_single_manifest_update_rejects_invalid_immutable_analysis_transition(
    tmp_path, corruption
):
    repository = _project_repository(tmp_path / "project.json")
    repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
            operation_id="op-2",
            analysis_revisions=("analysis-1", "analysis-2"),
            analysis_operation_ids={
                "analysis-1": "op-1",
                "analysis-2": "op-2",
            },
        ),
    )

    def corrupt(value: ProjectManifest) -> ProjectManifest:
        if corruption == "delete":
            return replace(
                value,
                analysis_revisions=("analysis-1",),
                analysis_operation_ids={"analysis-1": "op-1"},
            )
        if corruption == "reorder":
            return replace(
                value,
                analysis_revisions=("analysis-2", "analysis-1"),
            )
        if corruption == "rebind":
            return replace(
                value,
                analysis_operation_ids={
                    "analysis-1": "op-rebound",
                    "analysis-2": "op-2",
                },
            )
        if corruption == "in_place_rebind":
            value.analysis_operation_ids["analysis-1"] = "op-rebound"
            return value
        return replace(
            value,
            operation_id="op-publication",
            analysis_revisions=(*value.analysis_revisions, "analysis-3"),
            analysis_operation_ids={
                **value.analysis_operation_ids,
                "analysis-3": "op-not-publication",
            },
        )

    with pytest.raises(ValueError, match="immutable analysis transition"):
        repository.update("p1", expected_revision=0, mutate=corrupt)

    assert repository.load("p1").revision == 0


@pytest.mark.parametrize(
    "corruption",
    [
        "delete",
        "reorder",
        "rebind",
        "in_place_rebind",
        "wrong_new_operation",
    ],
)
def test_cross_manifest_publication_rejects_invalid_immutable_analysis_transition(
    tmp_path, corruption
):
    project_repository = _project_repository(tmp_path / "project.json")
    clips_repository = _clips_repository(tmp_path / "clips.json")
    project_repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
            operation_id="op-2",
            analysis_revisions=("analysis-1", "analysis-2"),
            analysis_operation_ids={
                "analysis-1": "op-1",
                "analysis-2": "op-2",
            },
        ),
    )
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )

    def corrupt(value: ProjectManifest, operation_id: str) -> ProjectManifest:
        if corruption == "delete":
            return replace(
                value,
                analysis_revisions=("analysis-1",),
                analysis_operation_ids={"analysis-1": "op-1"},
            )
        if corruption == "reorder":
            return replace(
                value,
                analysis_revisions=("analysis-2", "analysis-1"),
            )
        if corruption == "rebind":
            return replace(
                value,
                analysis_operation_ids={
                    "analysis-1": "op-rebound",
                    "analysis-2": "op-2",
                },
            )
        if corruption == "in_place_rebind":
            value.analysis_operation_ids["analysis-1"] = "op-rebound"
            return value
        return replace(
            value,
            analysis_revisions=(*value.analysis_revisions, "analysis-3"),
            analysis_operation_ids={
                **value.analysis_operation_ids,
                "analysis-3": f"not-{operation_id}",
            },
        )

    with pytest.raises(ValueError, match="immutable analysis transition"):
        publish_manifests(
            (
                ManifestMutation(project_repository, "p1", 0, corrupt),
                ManifestMutation(
                    clips_repository,
                    "p1",
                    0,
                    lambda value, _operation_id: value,
                ),
            )
        )

    assert project_repository.load("p1").revision == 0
    assert clips_repository.load("p1").revision == 0


def test_single_update_restamps_direct_candidate_to_active_pointer(tmp_path):
    repository = _project_repository(tmp_path / "project.json")
    repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
            operation_id="op-register-2",
            active_analysis_revision="analysis-1",
            active_analysis_operation_id="op-activate-1",
            candidate_analysis_revision="analysis-2",
            candidate_analysis_operation_id="op-register-2",
            analysis_revisions=("analysis-1", "analysis-2"),
            analysis_operation_ids={
                "analysis-1": "op-register-1",
                "analysis-2": "op-register-2",
            },
        ),
    )

    published = repository.update(
        "p1",
        expected_revision=0,
        mutate=lambda value: replace(
            value,
            active_analysis_revision="analysis-2",
            active_analysis_operation_id="op-register-2",
            candidate_analysis_revision=None,
            candidate_analysis_operation_id=None,
        ),
    )

    assert published.active_analysis_operation_id == published.operation_id
    assert published.active_analysis_operation_id != "op-register-2"
    assert published.analysis_operation_ids["analysis-2"] == "op-register-2"


def test_cross_publication_restamps_direct_candidate_to_active_pointer(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    clips_repository = _clips_repository(tmp_path / "clips.json")
    project_repository.create(
        "p1",
        expected_revision=-1,
        value=replace(
            ProjectManifest.new("p1", updated_at="2026-08-03T00:00:00Z"),
            operation_id="op-register-2",
            active_analysis_revision="analysis-1",
            active_analysis_operation_id="op-activate-1",
            candidate_analysis_revision="analysis-2",
            candidate_analysis_operation_id="op-register-2",
            analysis_revisions=("analysis-1", "analysis-2"),
            analysis_operation_ids={
                "analysis-1": "op-register-1",
                "analysis-2": "op-register-2",
            },
        ),
    )
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1",
            analysis_revision="analysis-1",
            updated_at="2026-08-03T00:00:00Z",
        ),
    )

    result = publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                0,
                lambda value, _operation_id: replace(
                    value,
                    active_analysis_revision="analysis-2",
                    active_analysis_operation_id="op-register-2",
                    candidate_analysis_revision=None,
                    candidate_analysis_operation_id=None,
                ),
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value, _operation_id: replace(
                    value, analysis_revision="analysis-2"
                ),
            ),
        )
    )

    published = project_repository.load("p1")
    assert published.active_analysis_operation_id == result.operation_id
    assert published.active_analysis_operation_id != "op-register-2"
    assert published.analysis_operation_ids["analysis-2"] == "op-register-2"


def test_prevalidated_candidate_is_not_decoded_again_during_publication(tmp_path):
    project_repository = _project_repository(tmp_path / "project.json")
    calls = 0

    def stateful_decoder(value):
        nonlocal calls
        calls += 1
        if calls > 3:
            raise ValueError("decoder called after complete prevalidation")
        return ClipsManifest.from_dict(value)

    clips_repository = AtomicJsonRepository(
        tmp_path / "clips.json",
        owner="clips",
        decoder=stateful_decoder,
    )
    _create_project(project_repository)
    clips_repository.create(
        "p1",
        expected_revision=-1,
        value=ClipsManifest.new(
            "p1", analysis_revision=None, updated_at="2026-08-03T00:00:00Z"
        ),
    )
    calls = 0

    publish_manifests(
        (
            ManifestMutation(
                project_repository,
                "p1",
                0,
                lambda value, _operation_id: value,
            ),
            ManifestMutation(
                clips_repository,
                "p1",
                0,
                lambda value, _operation_id: value,
            ),
        )
    )

    assert calls == 3
