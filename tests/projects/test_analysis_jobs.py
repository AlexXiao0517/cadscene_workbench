from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.analysis_worker import main as analysis_worker_main
from cadscene.projects.executor import LocalJobExecutor
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
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
    assert all(item.adapter_version == "1" for item in jobs)
    assert all(item.input_revision == "request-1" for item in jobs)
    assert all(len(item.input_fingerprint) == 64 for item in jobs)
    persisted = repositories.jobs.load("p1")
    assert tuple(item["job_id"] for item in persisted.jobs) == result.job_ids
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["job_ids"] == list(result.job_ids)
    assert analysis["operation_id"] == jobs[0].operation_id

    repeated = service.enqueue_analysis_jobs("p1")
    assert repeated.job_ids == result.job_ids


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
            output_fingerprint="c" * 64,
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
            output_fingerprint="d" * 64,
            outputs={"analysis_output": str(output), "analysis_revision": revision},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "success", finished.error
    project = repositories.project.load("p1")
    assert project.active_analysis_revision == revision
    assert repositories.clips.load("p1").clips[0].clip_id == "clip-0001"
    dataset_id = project.source_assets["cad"]["dataset_id"]
    assert dataset_id == f"p1-analysis-{revision}"
    assert (tmp_path / "data" / dataset_id / "design.json").is_file()
    assert (
        tmp_path
        / "projects"
        / "p1"
        / "analyses"
        / revision
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
            output_fingerprint="d" * 64,
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
            output_fingerprint="c" * 64,
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
            output_fingerprint="d" * 64,
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=video_attempt.number,
        claim_token=str(video_attempt.worker_claim_token),
    )
    assert finished.status == "success", finished.error
    dataset_id = f"p1-analysis-{revision}"
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
            output_fingerprint="c" * 64,
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
            output_fingerprint="d" * 64,
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert failed.status == "failed"
    assert repositories.project.load("p1").active_analysis_revision is None
    dataset_id = f"p1-analysis-{revision}"
    assert (tmp_path / "data" / dataset_id).is_dir()
    assert (tmp_path / "projects" / "p1" / "analyses" / revision).is_dir()
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
            output_fingerprint="d" * 64,
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
            output_fingerprint="d" * 64,
            outputs={"analysis_output": str(output)},
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )
    assert failed.status == "failed"
    dataset_id = f"p1-analysis-{revision}"
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
            output_fingerprint="d" * 64,
            outputs={"analysis_output": str(retry_output)},
        ),
        attempt_number=retry_attempt.number,
        claim_token=str(retry_attempt.worker_claim_token),
    )

    assert retried.status == "failed"
    assert "different content" in str(retried.error)
    assert repositories.project.load("p1").active_analysis_revision is None
