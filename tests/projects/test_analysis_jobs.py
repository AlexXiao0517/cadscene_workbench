from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

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
