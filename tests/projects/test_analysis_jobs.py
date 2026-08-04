from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

import pytest

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.analysis_adapters import tree_fingerprint
from cadscene.projects.analysis_publication import AnalysisArtifactPublisher
from cadscene.projects.analysis_worker import main as analysis_worker_main
from cadscene.projects.executor import LocalJobExecutor
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.recovery import reconcile_project
from cadscene.projects.service import ProjectService
from cadscene.projects.uploads import PublishedUpload
from cadscene.projects.workflow_adapters import default_workflow_adapters
from cadscene.workflow.data_import import load_dataset_manifest


def _service(tmp_path: Path):
    projects_root = tmp_path / "projects"
    repositories = project_repositories(projects_root)
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.json"
    video.write_bytes(b"video")
    cad.write_text("{}", encoding="utf-8")
    current = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {
                    "path": str(video),
                    "sha256": "a" * 64,
                    "original_filename": "source.mp4",
                },
                "cad": {
                    "path": str(cad),
                    "sha256": "b" * 64,
                    "original_filename": "design.json",
                },
                "_analysis": {"request_key": "request-1", "status": "queued"},
            },
            project_state="analyzing",
        ),
    )
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: "later",
        identity=iter((f"id-{index}" for index in range(20))).__next__,
    )
    return service, repositories, queue


def test_analysis_enqueue_persists_explicit_lightweight_dag(tmp_path: Path) -> None:
    service, repositories, queue = _service(tmp_path)

    result = service.enqueue_analysis_jobs("p1")

    jobs = queue.jobs()
    assert result.job_ids == tuple(item.job_id for item in jobs)
    assert [item.job_type for item in jobs] == ["cad_analysis", "video_analysis"]
    assert all(item.resource_class == "light_compute" for item in jobs)
    assert jobs[0].depends_on_job_ids == ()
    assert jobs[1].depends_on_job_ids == (jobs[0].job_id,)
    assert jobs[0].exclusive_key == "analysis:p1:cad"
    assert jobs[1].exclusive_key == "analysis:p1:video"
    assert all(item.adapter_name == "project_analysis" for item in jobs)
    assert all(item.adapter_version == "2" for item in jobs)
    assert all(item.input_revision == "request-1" for item in jobs)
    assert all(len(item.input_fingerprint) == 64 for item in jobs)
    persisted = repositories.jobs.load("p1")
    assert tuple(item["job_id"] for item in persisted.jobs) == result.job_ids
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["job_ids"] == list(result.job_ids)
    assert analysis["operation_id"] == jobs[0].operation_id

    repeated = service.enqueue_analysis_jobs("p1")
    assert repeated.job_ids == result.job_ids


@pytest.mark.parametrize("recorded_count", (0, 1))
def test_enqueue_repairs_incomplete_analysis_job_refs_without_rewriting_reused_jobs(
    tmp_path: Path, recorded_count: int
) -> None:
    service, repositories, queue = _service(tmp_path)
    first = service.enqueue_analysis_jobs("p1")
    service._publish_queue("p1")
    before = {item.job_id: item.to_dict() for item in queue.jobs()}
    project = repositories.project.load("p1")

    def remove_refs(value):
        assets = dict(value.source_assets)
        state = dict(assets["_analysis"])
        if recorded_count:
            state["job_ids"] = [first.job_ids[0]]
        else:
            state.pop("job_ids", None)
        assets["_analysis"] = state
        return replace(value, source_assets=assets)

    repositories.project.update(
        "p1", expected_revision=project.revision, mutate=remove_refs
    )

    repaired = service.enqueue_analysis_jobs("p1")

    assert repaired.job_ids == first.job_ids
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert tuple(analysis["job_ids"]) == first.job_ids
    durable = {
        str(item["job_id"]): item for item in repositories.jobs.load("p1").jobs
    }
    assert durable == before
    assert {item.job_id: item.to_dict() for item in queue.jobs()} == before


def test_enqueue_restores_reused_success_dag_without_busy_regression(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    first = service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {
                    key: item
                    for key, item in value.source_assets["_analysis"].items()
                    if key != "job_ids"
                },
            },
        ),
    )

    repaired = service.enqueue_analysis_jobs("p1")

    assert repaired.job_ids == first.job_ids
    restored = repositories.project.load("p1")
    assert restored.source_assets["_analysis"]["status"] == "success"
    assert restored.source_assets["_analysis"]["analysis_revision"] == revision
    assert restored.project_state == "ready"
    assert queue.claim_next_unstarted() is None
    preflight = service.preflight_trajectory_jobs("p1")
    assert "project analysis is still running" not in preflight.reasons.values()


def test_enqueue_complete_recorded_success_dag_repairs_legacy_busy_state(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    first = service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    before_jobs = {item.job_id: item.to_dict() for item in queue.jobs()}
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {
                    **value.source_assets["_analysis"],
                    "status": "queued",
                },
            },
            project_state="analyzing",
        ),
    )

    repaired = service.enqueue_analysis_jobs("p1")

    assert repaired.job_ids == first.job_ids
    restored = repositories.project.load("p1")
    assert restored.source_assets["_analysis"]["status"] == "success"
    assert restored.project_state == "ready"
    assert {item.job_id: item.to_dict() for item in queue.jobs()} == before_jobs
    assert {
        str(item["job_id"]): item
        for item in repositories.jobs.load("p1").jobs
    } == before_jobs


def test_enqueue_restores_reused_cancelled_dag_as_terminal(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    first = service.enqueue_analysis_jobs("p1")
    service.cancel_job("p1", first.job_ids[0])
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {
                    "request_key": "request-1",
                    "status": "queued",
                },
            },
            project_state="analyzing",
        ),
    )

    repaired = service.enqueue_analysis_jobs("p1")

    assert repaired.job_ids == first.job_ids
    restored = repositories.project.load("p1")
    assert restored.source_assets["_analysis"]["status"] == "cancelled"
    assert restored.project_state == "analysis_cancelled"
    assert queue.claim_next_unstarted() is None


def test_upload_restores_reused_success_dag_and_immutable_descriptor(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    from cadscene.projects import service as service_module

    project = repositories.project.load("p1")
    request_key = service_module._analysis_request_key_from_assets(
        project.source_assets
    )
    assert request_key is not None
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {"request_key": request_key, "status": "queued"},
            },
        ),
    )
    first = service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    project = repositories.project.load("p1")
    descriptor = project.source_assets["_analysis_revisions"][revision]
    assets = dict(project.source_assets)
    assets.pop("_analysis")
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(value, source_assets=assets),
    )
    video_path = Path(assets["video"]["path"])
    report = tmp_path / "same-video.validation.json"
    report.write_text("{}", encoding="utf-8")

    registered = service.register_uploaded_asset(
        "p1",
        PublishedUpload(
            project_id="p1",
            asset_type="video",
            original_filename="source.mp4",
            path=video_path,
            size_bytes=video_path.stat().st_size,
            sha256="a" * 64,
            validation={"decoded": True},
            validation_report_path=report,
        ),
        expected_revision=project.revision,
    )

    assert registered.analysis_job_ids == first.job_ids
    restored = repositories.project.load("p1")
    state = restored.source_assets["_analysis"]
    assert state["status"] == "success"
    assert state["analysis_revision"] == revision
    assert state["analysis_artifact_id"] == descriptor["analysis_artifact_id"]
    assert restored.project_state == "ready"
    assert queue.claim_next_unstarted() is None


def test_new_request_clears_previous_analysis_result_provenance(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _service(tmp_path)
    project = repositories.project.load("p1")
    stale_state = {
        **project.source_assets["_analysis"],
        "analysis_revision": "old-revision",
        "input_snapshot": {"request_key": "old-request"},
        "analysis_artifact_id": "video-analysis-old",
        "analysis_artifact_path": str(tmp_path / "old-artifact"),
    }
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={**value.source_assets, "_analysis": stale_state},
        ),
    )
    replacement = tmp_path / "new-source.mp4"
    replacement.write_bytes(b"new-source")
    report = tmp_path / "new-source.validation.json"
    report.write_text("{}", encoding="utf-8")

    service.register_uploaded_asset(
        "p1",
        PublishedUpload(
            project_id="p1",
            asset_type="video",
            original_filename="new-source.mp4",
            path=replacement,
            size_bytes=replacement.stat().st_size,
            sha256="e" * 64,
            validation={"decoded": True},
            validation_report_path=report,
        ),
        expected_revision=project.revision,
    )

    state = repositories.project.load("p1").source_assets["_analysis"]
    assert state["request_key"] != "request-1"
    for key in (
        "analysis_revision",
        "input_snapshot",
        "analysis_artifact_id",
        "analysis_artifact_path",
    ):
        assert key not in state


@pytest.mark.parametrize(
    "missing_key",
    ("input_snapshot", "analysis_artifact_id", "analysis_artifact_path"),
)
def test_reused_success_dag_with_incomplete_descriptor_fails_closed(
    tmp_path: Path, missing_key: str
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    project = repositories.project.load("p1")
    assets = dict(project.source_assets)
    revisions = dict(assets["_analysis_revisions"])
    descriptor = dict(revisions[revision])
    descriptor.pop(missing_key)
    revisions[revision] = descriptor
    state = dict(assets["_analysis"])
    state.pop("job_ids")
    assets.update({"_analysis_revisions": revisions, "_analysis": state})
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(value, source_assets=assets),
    )

    service.enqueue_analysis_jobs("p1")

    restored = repositories.project.load("p1")
    state = restored.source_assets["_analysis"]
    assert state["status"] == "failed"
    assert restored.project_state == "analysis_failed"
    assert "immutable descriptor" in str(state["error"])
    for key in (
        "analysis_revision",
        "input_snapshot",
        "analysis_artifact_id",
        "analysis_artifact_path",
    ):
        assert key not in state


@pytest.mark.parametrize(
    "invalid_kind",
    ("duplicate_video", "cross_project", "wrong_dependency", "wrong_type"),
)
def test_recorded_analysis_dag_is_validated_before_terminal_recovery(
    tmp_path: Path, invalid_kind: str
) -> None:
    service, repositories, queue = _service(tmp_path)
    canonical_ids, _revision = _complete_analysis(
        service, repositories, queue, tmp_path
    )
    cad = queue.get(canonical_ids[0])
    video = queue.get(canonical_ids[1])
    recorded_ids: tuple[str, str]
    if invalid_kind == "duplicate_video":
        recorded_ids = (video.job_id, video.job_id)
    else:
        fake = replace(
            video,
            job_id=f"fake-{invalid_kind}",
            project_id=("p2" if invalid_kind == "cross_project" else "p1"),
            job_type=("trajectory" if invalid_kind == "wrong_type" else "video_analysis"),
            depends_on_job_ids=(
                () if invalid_kind == "wrong_dependency" else (cad.job_id,)
            ),
            idempotency_key=f"fake-key-{invalid_kind}",
            exclusive_key=f"fake:{invalid_kind}",
        )
        queue.submit(fake)
        if fake.project_id == "p1":
            service._publish_queue("p1")
        recorded_ids = (cad.job_id, fake.job_id)
    before_queue = {item.job_id: item.to_dict() for item in queue.jobs()}
    before_order = queue.queue_order()
    before_jobs = repositories.jobs.load("p1")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {
                    **value.source_assets["_analysis"],
                    "status": "queued",
                    "job_ids": list(recorded_ids),
                },
            },
            project_state="analyzing",
        ),
    )

    repaired = service.enqueue_analysis_jobs("p1")

    assert repaired.job_ids == canonical_ids
    restored = repositories.project.load("p1")
    assert tuple(restored.source_assets["_analysis"]["job_ids"]) == canonical_ids
    assert restored.source_assets["_analysis"]["status"] == "success"
    assert {item.job_id: item.to_dict() for item in queue.jobs()} == before_queue
    assert queue.queue_order() == before_order
    after_jobs = repositories.jobs.load("p1")
    assert after_jobs.jobs == before_jobs.jobs
    assert after_jobs.queue_order == before_jobs.queue_order


@pytest.mark.parametrize(
    "descriptor_fault",
    ("empty_snapshot", "wrong_request_key", "artifact_mismatch", "video_identity"),
)
def test_reused_success_descriptor_identity_must_match_current_request(
    tmp_path: Path, descriptor_fault: str
) -> None:
    service, repositories, queue = _service(tmp_path)
    _job_ids, revision = _complete_analysis(
        service, repositories, queue, tmp_path
    )
    project = repositories.project.load("p1")
    assets = dict(project.source_assets)
    revisions = dict(assets["_analysis_revisions"])
    descriptor = dict(revisions[revision])
    snapshot = dict(descriptor["input_snapshot"])
    if descriptor_fault == "empty_snapshot":
        snapshot = {}
    elif descriptor_fault == "wrong_request_key":
        snapshot["request_key"] = "another-request"
    elif descriptor_fault == "artifact_mismatch":
        snapshot["analysis_artifact"] = {
            "artifact_id": "video-analysis-other",
            "path": str(tmp_path / "other-artifact"),
        }
    else:
        snapshot["video"] = {
            **snapshot["video"],
            "sha256": "f" * 64,
        }
    descriptor["input_snapshot"] = snapshot
    revisions[revision] = descriptor
    state = dict(assets["_analysis"])
    state.pop("job_ids")
    assets.update({"_analysis_revisions": revisions, "_analysis": state})
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(value, source_assets=assets),
    )

    service.enqueue_analysis_jobs("p1")

    restored = repositories.project.load("p1")
    state = restored.source_assets["_analysis"]
    assert state["status"] == "failed"
    assert restored.project_state == "analysis_failed"
    assert "immutable descriptor" in str(state["error"])


def test_descriptor_control_field_injection_cannot_override_submission_state(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    job_ids, revision = _complete_analysis(service, repositories, queue, tmp_path)
    project = repositories.project.load("p1")
    expected_request_key = project.source_assets["_analysis"]["request_key"]
    assets = dict(project.source_assets)
    revisions = dict(assets["_analysis_revisions"])
    revisions[revision] = {
        **revisions[revision],
        "request_key": "injected-request",
        "job_ids": ["injected-job"],
        "operation_id": "injected-operation",
    }
    state = dict(assets["_analysis"])
    state.pop("job_ids")
    assets.update({"_analysis_revisions": revisions, "_analysis": state})
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(value, source_assets=assets),
    )

    service.enqueue_analysis_jobs("p1")

    state = repositories.project.load("p1").source_assets["_analysis"]
    assert state["status"] == "success"
    assert state["request_key"] == expected_request_key
    assert tuple(state["job_ids"]) == job_ids
    assert state["operation_id"] != "injected-operation"


def test_analysis_enqueue_failure_never_leaves_queued_intent_without_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repositories, queue = _service(tmp_path)
    from cadscene.projects import service as service_module

    monkeypatch.setattr(
        service_module,
        "publish_manifests",
        lambda _mutations: (_ for _ in ()).throw(
            OSError("injected enqueue publication failure")
        ),
    )

    with pytest.raises(OSError, match="injected enqueue publication"):
        service.enqueue_analysis_jobs("p1")

    state = repositories.project.load("p1").source_assets["_analysis"]
    assert state["status"] == "failed"
    assert state["job_ids"] == []
    assert queue.jobs() == ()
    assert repositories.jobs.load("p1").jobs == ()


def _finish_cad(service: ProjectService, queue: LocalResourceQueue, tmp_path: Path):
    cad_job = queue.claim_next_unstarted()
    assert cad_job is not None and cad_job.job_type == "cad_analysis"
    attempt = cad_job.attempts[-1]
    dataset = Path(attempt.directory) / "scratch" / "data" / "p1"
    dataset.mkdir(parents=True)
    (dataset / "design.json").write_text("{}", encoding="utf-8")
    (dataset / "dataset_manifest.json").write_text(
        json.dumps({"dataset": "p1", "cad": {"status": "ready"}}),
        encoding="utf-8",
    )
    finished = service.finish_job(
        "p1",
        cad_job.job_id,
        AdapterResult.success(
            output_revision="cad-output",
            output_fingerprint=tree_fingerprint(dataset),
            outputs={"cad_dataset": str(dataset)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert finished.status == "success", finished.error
    return finished


def _video_output(job, tmp_path: Path) -> tuple[Path, str]:
    revision = f"analysis-{job.job_id}"
    output = Path(job.attempts[-1].directory) / "02_video_analysis" / revision
    output.mkdir(parents=True)
    (output / "clip_manifest.json").write_text(
        json.dumps(
            {
                "analysis_revision": revision,
                "clips": [
                    {
                        "project_id": "p1",
                        "clip_id": "clip-0001",
                        "analysis_revision": revision,
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 100,
                        "source_time_base": {"numerator": 1, "denominator": 25},
                        "interval_semantics": "half_open",
                        "scene_index": 1,
                        "segment_index": 1,
                        "recommended_workflow": "sfm_only",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return output, revision


def _complete_analysis(
    service: ProjectService,
    repositories,
    queue: LocalResourceQueue,
    tmp_path: Path,
) -> tuple[tuple[str, str], str]:
    result = service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    finished = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert finished.status == "success"
    assert repositories.project.load("p1").active_analysis_revision == revision
    return result.job_ids, revision


def test_video_finish_is_only_owner_that_publishes_cad_and_analysis(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    assert not (tmp_path / "data").exists()
    video_job = queue.claim_next_unstarted()
    assert video_job is not None and video_job.job_type == "video_analysis"
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]

    finished = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output), "analysis_revision": revision},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "success", finished.error
    project = repositories.project.load("p1")
    assert finished.publication_operation_id == project.active_analysis_operation_id
    assert finished.submission_operation_id
    assert project.active_analysis_revision == revision
    assert repositories.clips.load("p1").clips[0].clip_id == "clip-0001"
    snapshot = project.source_assets["_analysis"]["input_snapshot"]
    dataset_id = snapshot["cad"]["dataset_id"]
    cad_fingerprint = queue.jobs()[0].output_fingerprint
    assert dataset_id == f"cad-{cad_fingerprint}"
    assert (tmp_path / "data" / dataset_id / "design.json").is_file()
    assert (
        tmp_path
        / "projects"
        / "p1"
        / "analysis_artifacts"
        / f"video-analysis-{tree_fingerprint(output)}"
        / "02_video_analysis"
        / "clip_manifest.json"
    ).is_file()


def test_superseded_video_finish_never_publishes_candidate_or_dataset(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    current = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {"request_key": "request-2", "status": "queued"},
            },
        ),
    )
    attempt = video_job.attempts[-1]

    finished = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output), "analysis_revision": revision},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "stale_input"
    project = repositories.project.load("p1")
    assert project.active_analysis_revision is None
    assert project.candidate_analysis_revision is None
    assert not (tmp_path / "data").exists()


def test_real_cad_attempt_is_rebased_to_immutable_dataset_id(tmp_path: Path) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    cad_job = queue.claim_next_unstarted()
    assert cad_job is not None and cad_job.job_type == "cad_analysis"
    attempt = cad_job.attempts[-1]
    cad_path = Path(repositories.project.load("p1").source_assets["cad"]["path"])
    assert analysis_worker_main(
        [
            "cad",
            "--project-id",
            "p1",
            "--input",
            str(cad_path),
            "--original-filename",
            "design.json",
            "--attempt-dir",
            attempt.directory,
        ]
    ) == 0
    dataset = Path(attempt.directory) / "scratch" / "data" / "p1"
    service.finish_job(
        "p1",
        cad_job.job_id,
        AdapterResult.success(
            output_revision="cad-output",
            output_fingerprint=tree_fingerprint(dataset),
            outputs={"cad_dataset": str(dataset)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    video_attempt = video_job.attempts[-1]
    finished = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=video_attempt.number,
        claim_token=str(video_attempt.worker_claim_token),
    )
    assert finished.status == "success", finished.error
    dataset_id = f"cad-{tree_fingerprint(dataset)}"
    manifest = load_dataset_manifest(tmp_path, dataset_id)
    assert manifest["dataset"] == dataset_id
    assert manifest["cad"]["design_json"] == f"data/{dataset_id}/design.json"
    assert (tmp_path / manifest["cad"]["design_json"]).is_file()


def test_analysis_result_cannot_publish_output_outside_attempt(tmp_path: Path) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    cad_job = queue.claim_next_unstarted()
    assert cad_job is not None
    external = tmp_path / "untrusted" / "data" / "p1"
    external.mkdir(parents=True)
    (external / "dataset_manifest.json").write_text(
        json.dumps({"dataset": "p1", "cad": {"status": "ready"}}),
        encoding="utf-8",
    )
    attempt = cad_job.attempts[-1]

    finished = service.finish_job(
        "p1",
        cad_job.job_id,
        AdapterResult.success(
            output_revision="cad-output",
            output_fingerprint=tree_fingerprint(external),
            outputs={"cad_dataset": str(external)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "failed"
    assert "escapes its immutable attempt directory" in str(finished.error)
    assert repositories.project.load("p1").active_analysis_revision is None


def test_restore_marks_unverified_analysis_running_as_interrupted(tmp_path: Path) -> None:
    service, repositories, _queue = _service(tmp_path)
    result = service.enqueue_analysis_jobs("p1")
    service._publish_queue("p1")
    restarted_queue = LocalResourceQueue()
    restarted = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "restart",
    )

    restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    restored = restarted_queue.get(result.job_ids[0])
    assert restored.status == "interrupted"
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["status"] == "interrupted"


def test_retry_interrupted_analysis_updates_durable_project_state(tmp_path: Path) -> None:
    service, repositories, _queue = _service(tmp_path)
    result = service.enqueue_analysis_jobs("p1")
    service._publish_queue("p1")
    restarted_queue = LocalResourceQueue()
    restarted = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "restart",
    )
    restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    retried = restarted.retry_job("p1", result.job_ids[0])

    assert retried.status in {"queued", "running"}
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["status"] == "queued"


def test_retry_reuses_valid_orphan_publications_after_manifest_failure(
    tmp_path: Path, monkeypatch
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    from cadscene.projects import service as service_module

    real_publish = service_module.publish_manifests
    calls = 0

    def fail_first_publication(mutations):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected manifest publication failure")
        return real_publish(mutations)

    monkeypatch.setattr(service_module, "publish_manifests", fail_first_publication)
    failed = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert failed.status == "failed"
    assert repositories.project.load("p1").active_analysis_revision is None
    cad_fingerprint = queue.jobs()[0].output_fingerprint
    dataset_id = f"cad-{cad_fingerprint}"
    assert (tmp_path / "data" / dataset_id).is_dir()
    assert (
        tmp_path
        / "projects"
        / "p1"
        / "analysis_artifacts"
        / f"video-analysis-{tree_fingerprint(output)}"
    ).is_dir()
    queue.release_execution_claim(
        video_job.job_id,
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    retried = service.retry_job("p1", video_job.job_id)
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == retried.job_id
    retry_output, retry_revision = _video_output(claimed, tmp_path)
    retry_attempt = claimed.attempts[-1]
    finished = service.finish_job(
        "p1",
        claimed.job_id,
        AdapterResult.success(
            output_revision=retry_revision,
            output_fingerprint=tree_fingerprint(retry_output),
            outputs={"analysis_output": str(retry_output)},
        ),
        attempt_number=retry_attempt.number,
        claim_token=str(retry_attempt.worker_claim_token),
    )

    assert finished.status == "success", finished.error
    assert repositories.project.load("p1").active_analysis_revision == revision


def test_real_executor_failure_updates_durable_analysis_state(tmp_path: Path) -> None:
    service, repositories, queue = _service(tmp_path)
    cad_path = Path(repositories.project.load("p1").source_assets["cad"]["path"])
    cad_path.write_text("not-json", encoding="utf-8")
    result = service.enqueue_analysis_jobs("p1")

    finished = LocalJobExecutor(service).run_next()

    assert finished is not None
    assert finished.job_id == result.job_ids[0]
    assert finished.status == "failed"
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["status"] == "failed"
    assert analysis["error"]
    assert queue.get(result.job_ids[1]).status == "queued"


def test_retry_rejects_conflicting_orphan_cad_publication(
    tmp_path: Path, monkeypatch
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    from cadscene.projects import service as service_module

    real_publish = service_module.publish_manifests
    monkeypatch.setattr(
        service_module,
        "publish_manifests",
        lambda _mutations: (_ for _ in ()).throw(OSError("injected failure")),
    )
    failed = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert failed.status == "failed"
    cad_fingerprint = queue.jobs()[0].output_fingerprint
    dataset_id = f"cad-{cad_fingerprint}"
    formal_manifest = tmp_path / "data" / dataset_id / "dataset_manifest.json"
    payload = json.loads(formal_manifest.read_text(encoding="utf-8"))
    formal_manifest.write_text(
        json.dumps({**payload, "tampered": True}), encoding="utf-8"
    )
    monkeypatch.setattr(service_module, "publish_manifests", real_publish)
    queue.release_execution_claim(
        video_job.job_id,
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    service.retry_job("p1", video_job.job_id)
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    retry_output, retry_revision = _video_output(claimed, tmp_path)
    retry_attempt = claimed.attempts[-1]

    retried = service.finish_job(
        "p1",
        claimed.job_id,
        AdapterResult.success(
            output_revision=retry_revision,
            output_fingerprint=tree_fingerprint(retry_output),
            outputs={"analysis_output": str(retry_output)},
        ),
        attempt_number=retry_attempt.number,
        claim_token=str(retry_attempt.worker_claim_token),
    )

    assert retried.status == "failed"
    assert "different content" in str(retried.error)
    assert repositories.project.load("p1").active_analysis_revision is None


def test_analysis_activation_prefix_failure_recovers_one_operation_on_restart(
    tmp_path: Path, monkeypatch
) -> None:
    service, repositories, queue = _service(tmp_path)
    result = service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    original_publish = repositories.jobs._publish_prepared_unchecked
    failed_once = False

    def fail_jobs_prefix(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("injected jobs manifest crash")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        repositories.jobs, "_publish_prepared_unchecked", fail_jobs_prefix
    )

    pending = service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert pending.status == "success"
    monkeypatch.setattr(
        repositories.jobs, "_publish_prepared_unchecked", original_publish
    )
    reconcile_project("p1", repositories=repositories)
    restarted_queue = LocalResourceQueue()
    restarted = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "restart",
    )
    restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    project = repositories.project.load("p1")
    persisted_video = restarted_queue.get(result.job_ids[1])
    assert persisted_video.status == "success"
    assert project.active_analysis_revision == revision
    assert project.source_assets["_analysis"]["status"] == "success"
    assert project.project_state == "ready"
    persisted_job = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == result.job_ids[1]
    )
    operation_ids = {
        project.active_analysis_operation_id,
        repositories.clips.load("p1").operation_id,
        persisted_job["operation_id"],
        persisted_job["publication_operation_id"],
    }
    assert len(operation_ids) == 1
    assert persisted_job["submission_operation_id"]


def test_restore_of_two_successful_analysis_jobs_preserves_ready_state(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    video_job = queue.claim_next_unstarted()
    assert video_job is not None
    output, revision = _video_output(video_job, tmp_path)
    attempt = video_job.attempts[-1]
    service.finish_job(
        "p1",
        video_job.job_id,
        AdapterResult.success(
            output_revision=revision,
            output_fingerprint=tree_fingerprint(output),
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "restart",
    )

    restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    project = repositories.project.load("p1")
    assert project.source_assets["_analysis"]["status"] == "success"
    assert project.project_state == "ready"


def test_cancelled_analysis_has_terminal_project_state(tmp_path: Path) -> None:
    service, repositories, _queue = _service(tmp_path)
    result = service.enqueue_analysis_jobs("p1")

    cancelled = service.cancel_job("p1", result.job_ids[0])

    assert cancelled.status == "cancelled"
    project = repositories.project.load("p1")
    assert project.source_assets["_analysis"]["status"] == "cancelled"
    assert project.project_state == "analysis_cancelled"


def test_candidate_new_video_does_not_change_active_clip_export_input(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    service.enqueue_analysis_jobs("p1")
    _finish_cad(service, queue, tmp_path)
    first_video_job = queue.claim_next_unstarted()
    assert first_video_job is not None
    first_output, first_revision = _video_output(first_video_job, tmp_path)
    first_attempt = first_video_job.attempts[-1]
    service.finish_job(
        "p1",
        first_video_job.job_id,
        AdapterResult.success(
            output_revision=first_revision,
            output_fingerprint=tree_fingerprint(first_output),
            outputs={"analysis_output": str(first_output)},
        ),
        attempt_number=first_attempt.number,
        claim_token=str(first_attempt.worker_claim_token),
    )
    active_clip = repositories.clips.load("p1").clips[0]
    old_video = Path(active_clip.analysis["input_snapshot"]["video"]["path"])
    historical_jobs = {
        item.job_id: item.to_dict() for item in queue.jobs()
    }

    replacement = tmp_path / "video-new.mp4"
    replacement.write_bytes(b"new-video")
    report = tmp_path / "video-new.validation.json"
    report.write_text("{}", encoding="utf-8")
    project = repositories.project.load("p1")
    registered = service.register_uploaded_asset(
        "p1",
        PublishedUpload(
            project_id="p1",
            asset_type="video",
            original_filename="video-new.mp4",
            path=replacement,
            size_bytes=replacement.stat().st_size,
            sha256="e" * 64,
            validation={"decoded": True},
            validation_report_path=report,
        ),
        expected_revision=project.revision,
    )
    assert len(registered.analysis_job_ids) == 2
    durable_jobs = {
        str(item["job_id"]): item for item in repositories.jobs.load("p1").jobs
    }
    for job_id, before in historical_jobs.items():
        assert durable_jobs[job_id] == before
        assert queue.get(job_id).to_dict() == before
    _finish_cad(service, queue, tmp_path)
    candidate_video_job = queue.claim_next_unstarted()
    assert candidate_video_job is not None
    candidate_output, candidate_revision = _video_output(
        candidate_video_job, tmp_path
    )
    candidate_attempt = candidate_video_job.attempts[-1]
    service.finish_job(
        "p1",
        candidate_video_job.job_id,
        AdapterResult.success(
            output_revision=candidate_revision,
            output_fingerprint=tree_fingerprint(candidate_output),
            outputs={"analysis_output": str(candidate_output)},
        ),
        attempt_number=candidate_attempt.number,
        claim_token=str(candidate_attempt.worker_claim_token),
    )
    project = repositories.project.load("p1")
    assert project.candidate_analysis_revision == candidate_revision
    descriptor = project.source_assets["_analysis_revisions"][candidate_revision]
    candidate_artifact = Path(descriptor["analysis_artifact_path"])
    assert descriptor["analysis_artifact_id"] == candidate_artifact.name
    assert (candidate_artifact / "02_video_analysis" / "clip_manifest.json").is_file()
    assert descriptor["input_snapshot"]["video"]["path"] == str(replacement)
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "_analysis": {
                    key: item
                    for key, item in value.source_assets["_analysis"].items()
                    if key != "job_ids"
                },
            },
        ),
    )
    repaired_candidate = service.enqueue_analysis_jobs("p1")
    assert repaired_candidate.job_ids == registered.analysis_job_ids
    project = repositories.project.load("p1")
    assert project.source_assets["_analysis"]["status"] == "success"
    assert project.project_state == "analysis_candidate_ready"
    assert queue.claim_next_unstarted() is None
    still_active = repositories.clips.load("p1").clips[0]
    assert still_active.analysis_revision == first_revision
    export_job = service._new_export_job(
        "p1",
        still_active,
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=repositories.clips.load("p1").revision,
    )

    plan = service._prepare_clip_export(export_job)

    command = plan.commands[0]
    assert Path(command[command.index("--video") + 1]) == old_video
    assert old_video != replacement


def test_analysis_publisher_rejects_fingerprint_that_does_not_match_content(
    tmp_path: Path,
) -> None:
    cad_source = tmp_path / "attempt" / "data" / "p1"
    cad_source.mkdir(parents=True)
    (cad_source / "dataset_manifest.json").write_text(
        json.dumps({"dataset": "p1", "cad": {"status": "ready"}}),
        encoding="utf-8",
    )
    analysis_source = tmp_path / "attempt" / "analysis"
    analysis_source.mkdir()
    (analysis_source / "clip_manifest.json").write_text(
        json.dumps({"analysis_revision": "r1", "clips": [{}]}),
        encoding="utf-8",
    )
    publisher = AnalysisArtifactPublisher(
        storage_root=tmp_path,
        projects_root=tmp_path / "projects",
        identity=lambda: "id-1",
    )

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        publisher.publish(
            project_id="p1",
            cad_source=cad_source,
            cad_fingerprint=tree_fingerprint(cad_source),
            analysis_source=analysis_source,
            analysis_fingerprint="f" * 64,
        )

    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "projects" / "p1" / "analysis_artifacts").exists()


def test_tree_fingerprint_frames_paths_and_content_without_ambiguity(
    tmp_path: Path,
) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "zz_a").write_bytes(b"bc")
    (right / "zz_ab").write_bytes(b"c")

    assert tree_fingerprint(left) != tree_fingerprint(right)


def test_analysis_publisher_rehashes_final_staging_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    publisher = AnalysisArtifactPublisher(
        storage_root=tmp_path,
        projects_root=tmp_path / "projects",
        identity=lambda: "copy-check",
    )
    from cadscene.projects import analysis_publication as publication_module

    real_copytree = publication_module.shutil.copytree

    def copy_then_mutate(source_path, target_path, *args, **kwargs):
        copied = real_copytree(source_path, target_path, *args, **kwargs)
        (Path(target_path) / "manifest.json").write_text(
            '{"mutated":true}', encoding="utf-8"
        )
        return copied

    monkeypatch.setattr(publication_module.shutil, "copytree", copy_then_mutate)

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        publisher._publish_immutable_tree(source, tmp_path / "published")

    assert not (tmp_path / "published").exists()


def test_analysis_recovery_holds_publication_lock_against_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repositories, queue = _service(tmp_path)
    result = service.enqueue_analysis_jobs("p1")
    job = queue.get(result.job_ids[0])
    from cadscene.projects import service as service_module

    recovering = threading.Event()
    release = threading.Event()

    def fail_finish(*_args, **_kwargs):
        raise service_module._AnalysisPublicationPending(
            "p1", job.job_id, OSError("injected publication failure")
        )

    def block_recovery(_pending):
        recovering.set()
        assert release.wait(5)
        return queue.get(job.job_id)

    monkeypatch.setattr(service, "_finish_job_once", fail_finish)
    monkeypatch.setattr(service, "_recover_analysis_publication", block_recovery)
    finish_result: list[object] = []
    finish_thread = threading.Thread(
        target=lambda: finish_result.append(
            service.finish_job(
                "p1",
                job.job_id,
                AdapterResult.failed("ignored"),
                attempt_number=1,
                claim_token="ignored",
            )
        )
    )
    finish_thread.start()
    assert recovering.wait(5)

    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"replacement")
    report = tmp_path / "replacement.validation.json"
    report.write_text("{}", encoding="utf-8")
    project = repositories.project.load("p1")
    upload_result: list[object] = []
    upload_thread = threading.Thread(
        target=lambda: upload_result.append(
            service.register_uploaded_asset(
                "p1",
                PublishedUpload(
                    project_id="p1",
                    asset_type="video",
                    original_filename="replacement.mp4",
                    path=replacement,
                    size_bytes=replacement.stat().st_size,
                    sha256="e" * 64,
                    validation={"decoded": True},
                    validation_report_path=report,
                ),
                expected_revision=project.revision,
            )
        )
    )
    upload_thread.start()
    upload_thread.join(0.2)
    assert upload_thread.is_alive()

    release.set()
    finish_thread.join(5)
    upload_thread.join(5)
    assert finish_result and upload_result
