from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
from cadscene.projects.workflow_adapters import default_workflow_adapters


def clip(
    clip_id: str,
    *,
    workflow: str = "sfm_only",
    needs_review: bool = False,
) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 100,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "recommended_workflow": workflow,
            "needs_review": needs_review,
        }
    )


def service_with_clips(tmp_path: Path, clips: tuple[ClipDefinition, ...]):
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="2026-08-03T00:00:00Z")
    video = tmp_path / "source.mp4"
    video.write_bytes(b"mp4")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            register_analysis_revision(
                current, "analysis-1", operation_id="analysis-operation"
            ),
            source_assets={"video_path": str(video)},
        ),
    )
    current_clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current_clips.revision,
        mutate=lambda current: replace(
            current,
            analysis_revision="analysis-1",
            clips=clips,
        ),
    )
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:01Z",
    )
    return service, repositories, queue


def test_batch_preflight_groups_partial_eligibility_without_enqueueing(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path,
        (
            clip("ready"),
            clip("review", needs_review=True),
            clip("unsupported", workflow="srt_full_pose"),
        ),
    )

    result = service.preflight_trajectory_jobs("p1")

    assert result.eligible == ("ready",)
    assert result.needs_confirmation == ("review",)
    assert result.skipped == ("unsupported",)
    assert "interface-only" in result.reasons["unsupported"]
    assert repositories.jobs.load("p1").jobs == ()


def test_project_package_exports_task3_public_interfaces() -> None:
    import cadscene.projects as projects

    assert projects.TaskQueue is not None
    assert projects.LocalResourceQueue is not None
    assert projects.AdapterProgress is not None
    assert projects.AdapterResult is not None
    assert projects.WorkflowAdapter is not None
    assert projects.ProjectService is not None


def test_batch_creates_durable_jobs_but_capacity_starts_only_one_sfm(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(
        tmp_path, (clip("one"), clip("two"))
    )

    result = service.enqueue_trajectory_jobs("p1")

    assert result.enqueued_clip_ids == ("one", "two")
    jobs = repositories.jobs.load("p1")
    assert jobs.queue_order == tuple(job["job_id"] for job in jobs.jobs)
    assert len(jobs.jobs) == 4
    assert len(queue.running_ids()) == 1
    assert sorted(queue.status(job["job_id"]) for job in jobs.jobs) == [
        "queued",
        "queued",
        "queued",
        "running",
    ]
    solves = [job for job in jobs.jobs if job["job_type"] == "trajectory"]
    exports = [job for job in jobs.jobs if job["job_type"] == "clip_export"]
    assert len(solves) == len(exports) == 2
    assert all(len(job["depends_on_job_ids"]) == 1 for job in solves)
    for stored in jobs.jobs:
        assert {
            "depends_on_job_ids",
            "exclusive_key",
            "idempotency_key",
            "input_revision",
            "input_fingerprint",
            "adapter_name",
            "adapter_version",
            "output_revision",
            "operation_id",
            "attempts",
        } <= stored.keys()


def test_changed_input_marks_active_job_stale_input_and_never_publishes(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(tmp_path, (clip("one"),))
    enqueued = service.enqueue_trajectory_jobs("p1")
    job_id = enqueued.job_ids[0]
    export_id = next(
        item["job_id"]
        for item in repositories.jobs.load("p1").jobs
        if item["job_type"] == "clip_export"
    )
    export = _queue.get(export_id)
    service.finish_job(
        "p1",
        export_id,
        AdapterResult.success(
            output_revision="export-1",
            output_fingerprint="export-fingerprint",
            outputs={"video": "attempt/clip.mp4"},
        ),
        current_fingerprint=export.input_fingerprint,
    )
    assert _queue.status(job_id) == "running"
    result = AdapterResult.success(
        output_revision="output-1",
        output_fingerprint="output-fingerprint",
        outputs={"trajectory": "attempt/camera_trajectory.json"},
    )

    finished = service.finish_job(
        "p1",
        job_id,
        result,
        current_fingerprint="changed-input",
    )

    assert finished.status == "stale_input"
    stored = next(
        item for item in repositories.jobs.load("p1").jobs if item["job_id"] == job_id
    )
    assert stored["status"] == "stale_input"
    assert stored["output_revision"] is None
    assert stored.get("published_outputs", {}) == {}


def test_attempt_directories_are_immutable_and_input_scoped(tmp_path: Path) -> None:
    service, repositories, _queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    trajectory_job_id = next(
        item["job_id"]
        for item in repositories.jobs.load("p1").jobs
        if item["job_type"] == "trajectory"
    )
    service.cancel_job("p1", trajectory_job_id)

    retried = service.retry_job("p1", trajectory_job_id)

    assert retried.attempts[0].directory.endswith("attempt-1")
    assert retried.attempts[1].directory.endswith("attempt-2")
    assert retried.attempts[0].directory != retried.attempts[1].directory
    assert len(repositories.jobs.load("p1").jobs) == 2


def test_repeated_enqueue_reuses_jobs_without_orphan_attempt_directories(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(tmp_path, (clip("one"),))
    first = service.enqueue_trajectory_jobs("p1")

    repeated = service.enqueue_trajectory_jobs("p1")

    assert repeated.job_ids == first.job_ids
    assert len(repositories.jobs.load("p1").jobs) == 2
    job_roots = list((tmp_path / "projects/p1/jobs").iterdir())
    assert len(job_roots) == 2


def test_idempotency_isolates_changed_relevant_manifest_revision(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(tmp_path, (clip("one"),))
    first = service.enqueue_trajectory_jobs("p1")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={**current.source_assets, "video_revision": "v2"},
        ),
    )

    changed = service.enqueue_trajectory_jobs("p1")

    assert changed.job_ids != first.job_ids
    assert len(repositories.jobs.load("p1").jobs) == 4


def test_service_restart_persists_unverifiable_running_job_as_interrupted(
    tmp_path: Path,
) -> None:
    service, repositories, original_queue = service_with_clips(
        tmp_path, (clip("one"),)
    )
    service.enqueue_trajectory_jobs("p1")
    original_order = original_queue.queue_order()
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )

    restored = restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    assert restored.queue_order() == original_order
    assert restored.status(original_order[0]) == "interrupted"
    stored = repositories.jobs.load("p1")
    assert stored.queue_order == original_order
    assert stored.jobs[0]["status"] == "interrupted"
