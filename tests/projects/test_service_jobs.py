from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import threading

import pytest

from cadscene.projects.adapters import AdapterProgress

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, register_analysis_revision
from cadscene.projects.queue import AttemptRecord, LocalResourceQueue, QueueJob
from cadscene.projects.repositories import RevisionConflict
from cadscene.projects.service import ProjectService
from cadscene.projects.workflow_adapters import default_workflow_adapters
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp
import cadscene.projects.service as service_module


def clip(
    clip_id: str,
    *,
    workflow: str = "sfm_only",
    needs_review: bool = False,
    project_id: str = "p1",
) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": project_id,
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


def add_project(
    repositories,
    tmp_path: Path,
    project_id: str,
    clips: tuple[ClipDefinition, ...],
) -> None:
    repositories.create_project(project_id, updated_at="2026-08-03T00:00:00Z")
    video = tmp_path / f"{project_id}.mp4"
    video.write_bytes(b"mp4")
    project = repositories.project.load(project_id)
    repositories.project.update(
        project_id,
        expected_revision=project.revision,
        mutate=lambda current: replace(
            register_analysis_revision(
                current, "analysis-1", operation_id=f"analysis-{project_id}"
            ),
            source_assets={"video_path": str(video)},
        ),
    )
    current_clips = repositories.clips.load(project_id)
    repositories.clips.update(
        project_id,
        expected_revision=current_clips.revision,
        mutate=lambda current: replace(
            current,
            analysis_revision="analysis-1",
            clips=clips,
        ),
    )


def test_job_runtime_reuses_current_attempt_workflow_status_and_log(
    tmp_path: Path,
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("clip-1"),))
    attempt_dir = tmp_path / "projects" / "p1" / "jobs" / "job-1" / "attempt-1"
    run_dir = attempt_dir / "p1" / "clip-1"
    run_dir.mkdir(parents=True)
    (run_dir / "job_status.json").write_text(
        '{"status":"running","operation":"sfm","stages":{"sfm":'
        '{"status":"running","progress":0.52,"message":"正在进行顺序匹配"}}}',
        encoding="utf-8",
    )
    log_path = attempt_dir / "adapter.log"
    log_path.write_text("first\n正在提取特征\n正在进行顺序匹配\n", encoding="utf-8")
    queue.submit(
        QueueJob(
            job_id="job-1",
            project_id="p1",
            clip_id="clip-1",
            job_type="trajectory",
            resource_class="heavy_compute",
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=(),
            exclusive_key="trajectory:p1:clip-1",
            idempotency_key="runtime-job-1",
            input_revision="analysis-1",
            input_fingerprint="input-v1",
            adapter_name="sfm_only",
            adapter_version="1",
            output_revision=None,
            operation_id="operation-job-1",
            attempts=(
                AttemptRecord(
                    number=1,
                    directory=str(attempt_dir),
                    log_path=str(log_path),
                ),
            ),
        )
    )

    runtime = service.job_runtime("p1", "job-1", tail=2)

    assert runtime["job_id"] == "job-1"
    assert runtime["workflow_status"]["stages"]["sfm"]["progress"] == 0.52
    assert runtime["lines"] == ["正在提取特征", "正在进行顺序匹配"]


def test_job_runtime_rejects_job_owned_by_another_project(tmp_path: Path) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("clip-1"),))
    attempt_dir = tmp_path / "projects" / "p2" / "jobs" / "job-2" / "attempt-1"
    queue.submit(
        QueueJob(
            job_id="job-2",
            project_id="p2",
            clip_id="clip-1",
            job_type="trajectory",
            resource_class="heavy_compute",
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=(),
            exclusive_key=None,
            idempotency_key="runtime-job-2",
            input_revision="analysis-1",
            input_fingerprint="input-v1",
            adapter_name="sfm_only",
            adapter_version="1",
            output_revision=None,
            operation_id="operation-job-2",
            attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
        )
    )

    with pytest.raises(KeyError, match="does not belong"):
        service.job_runtime("p1", "job-2")


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
    assert "CAD georeference" in result.reasons["unsupported"]
    assert repositories.jobs.load("p1").jobs == ()


def test_fixed_track_srt_does_not_require_legacy_clip_analysis_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    srt_clip = ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "srt-track",
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 100,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "recommended_workflow": "srt_fixed_track_visual_pose",
            "needs_review": True,
            "srt_coverage": {"trajectory_coverage": 1.0},
        },
        manual_definition={
            "srt_fixed_track_visual_pose": {"horizontal_fov_deg": 72}
        },
    )
    service, repositories, _queue = service_with_clips(tmp_path, (srt_clip,))
    srt = tmp_path / "source.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,040\nGPS\n", encoding="utf-8")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={**current.source_assets, "srt_path": str(srt)},
        ),
    )
    monkeypatch.setattr(
        service_module,
        "_confirmed_cad_georeference",
        lambda _assets: {"confirmed": True},
    )
    result = service.preflight_trajectory_jobs("p1")

    assert result.eligible == ("srt-track",)
    assert result.needs_confirmation == ()
    assert result.reasons["srt-track"] != "clip analysis requires confirmation"

    exported_video = tmp_path / "srt-track.mp4"
    exported_video.write_bytes(b"mp4")
    exported_frame_map = tmp_path / "clip_frame_map.json"
    exported_frame_map.write_text("{}", encoding="utf-8")
    export_job = QueueJob(
        job_id="export-srt-track",
        project_id="p1",
        clip_id="srt-track",
        job_type="clip_export",
        resource_class="media_io",
        status="success",
        stage="success",
        priority=0,
        depends_on_job_ids=(),
        exclusive_key="export:p1",
        idempotency_key="export-srt-track",
        input_revision="analysis-1",
        input_fingerprint="export-input",
        adapter_name="clip_export",
        adapter_version="2",
        output_revision="clip-export-1",
        operation_id="export-operation",
        attempts=(AttemptRecord(number=1, directory=str(tmp_path / "attempt")),),
        output_validated=True,
        validated_input_fingerprint="export-input",
        published_outputs={
            "video:srt-track": str(exported_video),
            "frame_map:srt-track": str(exported_frame_map),
        },
    )
    jobs = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=jobs.revision,
        mutate=lambda current: replace(
            current,
            jobs=(export_job.to_dict(),),
            queue_order=(export_job.job_id,),
        ),
    )

    after_export = service.preflight_trajectory_jobs("p1")

    assert "srt-track" not in after_export.reasons


def test_whole_source_srt_solve_runs_independently_from_preview_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    srt_clip = ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "srt-whole",
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 100,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "recommended_workflow": "srt_fixed_track_visual_pose",
            "needs_review": False,
            "srt_coverage": {"trajectory_coverage": 1.0},
            "start_boundary": {"reasons": ["source_start"]},
            "end_boundary": {"reasons": ["source_end"]},
        },
        manual_definition={
            "srt_fixed_track_visual_pose": {"horizontal_fov_deg": 72}
        },
    )
    service, repositories, queue = service_with_clips(tmp_path, (srt_clip,))
    srt = tmp_path / "source.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,040\nGPS\n", encoding="utf-8")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={**current.source_assets, "srt_path": str(srt)},
        ),
    )
    monkeypatch.setattr(
        service_module,
        "_confirmed_cad_georeference",
        lambda _assets: {"confirmed": True},
    )
    monkeypatch.setattr(
        service_module,
        "_validated_whole_source_frame_index",
        lambda _clip, _assets: object(),
    )

    result = service.enqueue_trajectory_jobs("p1")

    trajectory = queue.get(result.job_ids[0])
    preview_export = next(job for job in queue.jobs() if job.job_type == "clip_export")
    assert trajectory.depends_on_job_ids == ()
    assert trajectory.request_parameters["whole_source_media"] is True
    assert preview_export.clip_id == trajectory.clip_id
    assert {trajectory.status, preview_export.status} == {"running"}


def test_whole_source_srt_media_builds_exact_attempt_local_frame_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = replace(
        clip("whole", workflow="srt_full_pose"),
        analysis={
            **clip("whole", workflow="srt_full_pose").analysis,
            "start_boundary": {"reasons": ["source_start"]},
            "end_boundary": {"reasons": ["source_end"]},
        },
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    index = DecodedFrameIndex(
        Fraction(1, 25),
        tuple(
            DecodedFrameTimestamp(
                ordinal=ordinal,
                pts=ordinal * 4,
                duration_pts=4,
                timestamp_source="pts",
            )
            for ordinal in range(25)
        ),
    )
    monkeypatch.setattr(service_module, "probe_fast_frame_index", lambda _path: index)

    video, frame_map = service_module._prepare_whole_source_srt_media(
        selected,
        {
            "video": {
                "path": str(source),
                "sha256": sha256(source.read_bytes()).hexdigest(),
            }
        },
        tmp_path / "attempt",
    )

    assert video == source
    payload = __import__("json").loads(frame_map.read_text(encoding="utf-8"))
    assert payload["full_source_partition"] is True
    assert payload["clips"][0]["clip_id"] == "whole"
    assert len(payload["clips"][0]["frames"]) == 25


def test_whole_source_srt_decoupling_requires_matching_asset_digest_and_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = replace(
        clip("whole", workflow="srt_full_pose"),
        analysis={
            **clip("whole", workflow="srt_full_pose").analysis,
            "start_boundary": {"reasons": ["source_start"]},
            "end_boundary": {"reasons": ["source_end"]},
        },
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"verified-video")
    exact_index = DecodedFrameIndex(
        Fraction(1, 25),
        tuple(
            DecodedFrameTimestamp(
                ordinal=ordinal,
                pts=ordinal,
                duration_pts=1,
                timestamp_source="pts",
            )
            for ordinal in range(100)
        ),
    )
    monkeypatch.setattr(
        service_module, "probe_fast_frame_index", lambda _path: exact_index
    )
    valid_assets = {
        "video": {
            "path": str(source),
            "sha256": sha256(source.read_bytes()).hexdigest(),
            "size_bytes": source.stat().st_size,
        }
    }

    assert service_module._uses_whole_source_srt_media(
        selected, (selected,), valid_assets
    ) is True
    assert service_module._uses_whole_source_srt_media(
        selected,
        (selected,),
        {"video": {**valid_assets["video"], "sha256": "0" * 64}},
    ) is False
    assert service_module._uses_whole_source_srt_media(
        selected, (selected,), {"video_path": str(source)}
    ) is False


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
    assert len(solves) == 2
    assert len(exports) == 2
    assert all(len(job["depends_on_job_ids"]) == 1 for job in solves)
    export_by_clip = {job["clip_id"]: job["job_id"] for job in exports}
    assert {
        job["clip_id"]: job["depends_on_job_ids"][0] for job in solves
    } == export_by_clip
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


def test_batch_routes_pure_rotation_selection_to_opengv_adapter(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "rotation-clip.mp4"
    physical.write_bytes(b"clip")
    selected = clip("rotation", workflow="pure_rotation")
    selected = replace(
        selected,
        analysis={**selected.analysis, "physical_mp4_path": str(physical)},
    )
    service, _repositories, queue = service_with_clips(tmp_path, (selected,))

    result = service.enqueue_trajectory_jobs("p1", clip_ids=("rotation",))

    assert len(result.job_ids) == 1
    solve = queue.get(result.job_ids[0])
    assert solve.job_type == "trajectory"
    assert solve.adapter_name == "pure_rotation"
    assert solve.resource_class == "heavy_compute"


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
    claimed_export = _queue.claim_next_unstarted()
    assert claimed_export is not None and claimed_export.job_id == export_id
    export_lease = claimed_export.attempts[-1]
    service.finish_job(
        "p1",
        export_id,
        AdapterResult.success(
            output_revision="export-1",
            output_fingerprint="export-fingerprint",
            outputs={"video": "attempt/clip.mp4"},
        ),
        current_fingerprint=export.input_fingerprint,
        attempt_number=export_lease.number,
        claim_token=str(export_lease.worker_claim_token),
    )
    assert _queue.status(job_id) == "running"
    claimed_solve = _queue.claim_next_unstarted()
    assert claimed_solve is not None and claimed_solve.job_id == job_id
    solve_lease = claimed_solve.attempts[-1]
    (tmp_path / "source.mp4").write_bytes(b"changed-source")
    result = AdapterResult.success(
        output_revision="output-1",
        output_fingerprint="output-fingerprint",
        outputs={"trajectory": "attempt/camera_trajectory.json"},
    )

    finished = service.finish_job(
        "p1",
        job_id,
        result,
        current_fingerprint=_queue.get(job_id).input_fingerprint,
        attempt_number=solve_lease.number,
        claim_token=str(solve_lease.worker_claim_token),
    )

    assert finished.status == "stale_input"
    stored = next(
        item for item in repositories.jobs.load("p1").jobs if item["job_id"] == job_id
    )
    assert stored["status"] == "stale_input"
    assert stored["output_revision"] is None
    assert stored.get("published_outputs", {}) == {}


def test_finish_recomputes_authoritative_identity_instead_of_trusting_caller(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    export_id = queue.running_ids()[0]
    old = queue.get(export_id).input_fingerprint
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == export_id
    lease = claimed.attempts[-1]
    (tmp_path / "source.mp4").write_bytes(b"new-source-content")

    finished = service.finish_job(
        "p1",
        export_id,
        AdapterResult.success(
            output_revision="forged-output",
            output_fingerprint="forged-fingerprint",
            outputs={"video:one": "old-input.mp4"},
        ),
        current_fingerprint=old,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "stale_input"
    stored = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == export_id
    )
    assert stored["output_revision"] is None
    assert stored["published_outputs"] == {}


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


def test_batch_enqueue_retries_cancelled_trajectory_as_a_new_attempt(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    first = service.enqueue_trajectory_jobs("p1")
    trajectory_job_id = first.job_ids[0]
    service.cancel_job("p1", trajectory_job_id)

    repeated = service.enqueue_trajectory_jobs("p1")

    retried = queue.get(trajectory_job_id)
    assert repeated.job_ids == (trajectory_job_id,)
    assert retried.status in {"queued", "preparing", "running"}
    assert [attempt.number for attempt in retried.attempts] == [1, 2]
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
    service, repositories, original_queue = service_with_clips(tmp_path, (clip("one"),))
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


def test_recovery_invalidates_old_success_and_supersedes_dependents(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    export_id = queue.running_ids()[0]
    export = queue.get(export_id)
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == export_id
    lease = claimed.attempts[-1]
    service.finish_job(
        "p1",
        export_id,
        AdapterResult.success(
            output_revision="export-1",
            output_fingerprint="export-fingerprint",
            outputs={"video:one": "attempt/one.mp4"},
        ),
        current_fingerprint=export.input_fingerprint,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    solve_id = next(
        item.job_id for item in queue.jobs() if item.job_type == "trajectory"
    )
    assert queue.status(solve_id) == "running"
    (tmp_path / "source.mp4").write_bytes(b"source-revision-2")
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:03Z",
    )

    restored = restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    assert restored.status(export_id) == "stale_input"
    assert restored.status(solve_id) == "superseded"
    assert restored.running_ids() == []
    stored = {item["job_id"]: item for item in repositories.jobs.load("p1").jobs}
    assert stored[export_id]["published_outputs"] == {}
    assert stored[solve_id]["output_revision"] is None


def test_restoring_second_project_merges_without_losing_global_queue_state(
    tmp_path: Path,
) -> None:
    service, repositories, original = service_with_clips(tmp_path, (clip("one"),))
    add_project(
        repositories,
        tmp_path,
        "p2",
        (clip("two", project_id="p2"),),
    )
    service.enqueue_trajectory_jobs("p1")
    service.enqueue_trajectory_jobs("p2")
    p1_ids = {job.job_id for job in original.jobs() if job.project_id == "p1"}
    p2_ids = {job.job_id for job in original.jobs() if job.project_id == "p2"}
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:04Z",
    )

    restarted.restore_jobs("p1", process_probe=lambda _pid: None)
    merged = restarted.restore_jobs("p2", process_probe=lambda _pid: None)

    assert {job.job_id for job in merged.jobs()} == p1_ids | p2_ids
    assert {job.project_id for job in merged.jobs()} == {"p1", "p2"}
    assert len(merged.running_ids()) <= 1
    assert {item["job_id"] for item in repositories.jobs.load("p1").jobs} == p1_ids
    assert {item["job_id"] for item in repositories.jobs.load("p2").jobs} == p2_ids


def test_requested_clip_export_plan_never_contains_unselected_clip(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(
        tmp_path, (clip("one"), clip("two"))
    )

    result = service.enqueue_trajectory_jobs("p1", clip_ids=("one",))

    assert result.enqueued_clip_ids == ("one",)
    exports = [item for item in queue.jobs() if item.job_type == "clip_export"]
    assert len(exports) == 1 and exports[0].clip_id == "one"
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == exports[0].job_id
    lease = claimed.attempts[-1]
    plan = service.prepare_job_execution(
        "p1",
        exports[0].job_id,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    command = plan.commands[0]
    assert "--allow-subset" in command
    payload = __import__("json").loads(
        Path(command[command.index("--manifest") + 1]).read_text(encoding="utf-8")
    )
    assert [item["clip_id"] for item in payload["clips"]] == ["one"]
    assert not any(item.clip_id == "two" for item in exports)
    assert len(repositories.jobs.load("p1").jobs) == 2


def test_prepare_execution_holds_clip_snapshot_until_plan_is_built(
    tmp_path: Path, monkeypatch
) -> None:
    service, repositories, queue = service_with_clips(
        tmp_path, (clip("one"),)
    )
    service.enqueue_trajectory_jobs("p1")
    export = next(item for item in queue.jobs() if item.job_type == "clip_export")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == export.job_id
    lease = claimed.attempts[-1]
    plan_entered = threading.Event()
    release_plan = threading.Event()
    original_prepare = service._prepare_clip_export

    def blocking_prepare(job):
        plan_entered.set()
        assert release_plan.wait(5)
        return original_prepare(job)

    monkeypatch.setattr(service, "_prepare_clip_export", blocking_prepare)
    plans: list[object] = []
    prepare_thread = threading.Thread(
        target=lambda: plans.append(
            service.prepare_job_execution(
                "p1",
                export.job_id,
                attempt_number=lease.number,
                claim_token=str(lease.worker_claim_token),
            )
        )
    )
    prepare_thread.start()
    assert plan_entered.wait(5)

    mutation_finished = threading.Event()

    def mutate_clip() -> None:
        current = repositories.clips.load("p1")
        repositories.clips.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                clips=(
                    replace(
                        value.clips[0],
                        workflow_override="pure_rotation",
                        resolved_workflow="pure_rotation",
                    ),
                ),
            ),
        )
        mutation_finished.set()

    mutation_thread = threading.Thread(target=mutate_clip)
    mutation_thread.start()
    mutation_thread.join(0.2)
    assert not mutation_finished.is_set()

    release_plan.set()
    prepare_thread.join(5)
    mutation_thread.join(5)
    assert plans and mutation_finished.is_set()


def test_existing_physical_clip_skips_export_without_exporting_other_clips(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "one.mp4"
    frame_map = tmp_path / "one-frame-map.json"
    physical.write_bytes(b"clip")
    frame_map.write_text("{}", encoding="utf-8")
    one = clip("one")
    one = replace(
        one,
        analysis={
            **one.analysis,
            "physical_mp4_path": str(physical),
            "frame_map_path": str(frame_map),
        },
    )
    service, _repositories, queue = service_with_clips(tmp_path, (one, clip("two")))

    service.enqueue_trajectory_jobs("p1", clip_ids=("one",))

    assert [item.job_type for item in queue.jobs()] == ["trajectory"]


def test_finish_waits_for_clip_mutation_and_observes_new_fingerprint(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    export = next(item for item in queue.jobs() if item.job_type == "clip_export")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == export.job_id
    lease = claimed.attempts[-1]
    mutation_entered = threading.Event()
    allow_mutation = threading.Event()

    def mutate(current):
        mutation_entered.set()
        assert allow_mutation.wait(5)
        changed = replace(
            current.clips[0],
            manual_definition={"changed": True},
        )
        return replace(current, clips=(changed,))

    mutation_thread = threading.Thread(
        target=lambda: repositories.clips.update(
            "p1",
            expected_revision=repositories.clips.load("p1").revision,
            mutate=mutate,
        )
    )
    mutation_thread.start()
    assert mutation_entered.wait(5)
    result_box: list[object] = []
    finish_thread = threading.Thread(
        target=lambda: result_box.append(
            service.finish_job(
                "p1",
                export.job_id,
                AdapterResult.success(
                    output_revision="old", output_fingerprint="old", outputs={}
                ),
                attempt_number=lease.number,
                claim_token=lease.worker_claim_token,
            )
        )
    )
    finish_thread.start()
    assert finish_thread.is_alive()
    allow_mutation.set()
    mutation_thread.join(5)
    finish_thread.join(5)

    assert result_box and result_box[0].status == "stale_input"
    stored = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == export.job_id
    )
    assert stored["status"] == "stale_input"
    assert stored["published_outputs"] == {}


def test_adopted_exit_is_published_as_interrupted_by_project_service(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    observed = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )
    restarted.restore_jobs("p1", process_probe=lambda _pid: observed or None)
    observed.clear()

    assert restarted.reap_adopted_jobs() == (claimed.job_id,)

    stored = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == claimed.job_id
    )
    assert stored["status"] == "interrupted"
    assert "completion sidecar" in stored["error"]


def test_adopted_reap_publishes_cross_project_job_scheduled_by_released_slot(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    add_project(
        repositories,
        tmp_path,
        "p2",
        (clip("two", project_id="p2"),),
    )
    service.enqueue_trajectory_jobs("p1")
    service.enqueue_trajectory_jobs("p2")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.project_id == "p1"
    lease = claimed.attempts[-1]
    observed = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )
    restarted.restore_jobs("p1", process_probe=lambda _pid: observed or None)
    restarted.restore_jobs("p2", process_probe=lambda _pid: observed or None)
    p2_job_id = next(
        job.job_id
        for job in restarted.queue.jobs()
        if job.project_id == "p2" and job.job_type == "clip_export"
    )
    assert restarted.queue.status(p2_job_id) == "queued"
    observed.clear()

    assert restarted.reap_adopted_jobs() == (claimed.job_id,)

    stored_p2 = next(
        item
        for item in repositories.jobs.load("p2").jobs
        if item["job_id"] == p2_job_id
    )
    assert restarted.queue.status(p2_job_id) == "running"
    assert stored_p2["status"] == "running"


def test_cross_project_schedule_is_unclaimable_until_failed_publication_resyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    add_project(
        repositories,
        tmp_path,
        "p2",
        (clip("two", project_id="p2"),),
    )
    service.enqueue_trajectory_jobs("p1")
    service.enqueue_trajectory_jobs("p2")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.project_id == "p1"
    lease = claimed.attempts[-1]
    observed = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )
    restarted.restore_jobs("p1", process_probe=lambda _pid: observed or None)
    restarted.restore_jobs("p2", process_probe=lambda _pid: observed or None)
    p2_job_id = next(
        item.job_id
        for item in restarted.queue.jobs()
        if item.project_id == "p2" and item.job_type == "clip_export"
    )
    original_publish = repositories.jobs._publish_prepared_unchecked
    failed = False

    def fail_p2_once(project_id, *args, **kwargs):
        nonlocal failed
        if project_id == "p2" and not failed:
            failed = True
            raise RuntimeError("p2 publication failed")
        return original_publish(project_id, *args, **kwargs)

    monkeypatch.setattr(repositories.jobs, "_publish_prepared_unchecked", fail_p2_once)
    observed.clear()

    with pytest.raises(RuntimeError, match="p2 publication failed"):
        restarted.reap_adopted_jobs()

    assert restarted.queue.status(p2_job_id) == "running"
    assert restarted.queue.claim_next_unstarted() is None
    assert (
        next(
            item
            for item in repositories.jobs.load("p2").jobs
            if item["job_id"] == p2_job_id
        )["status"]
        == "queued"
    )

    assert restarted.reap_adopted_jobs() == ()
    claimed_after_resync = restarted.queue.claim_next_unstarted()
    assert claimed_after_resync is not None
    assert claimed_after_resync.job_id == p2_job_id
    assert (
        next(
            item
            for item in repositories.jobs.load("p2").jobs
            if item["job_id"] == p2_job_id
        )["status"]
        == "running"
    )


def test_pending_project_publications_continue_after_one_project_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, _queue = service_with_clips(tmp_path, (clip("one"),))
    add_project(repositories, tmp_path, "p2", (clip("two", project_id="p2"),))
    add_project(repositories, tmp_path, "p3", (clip("three", project_id="p3"),))
    restarted = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )
    for project_id in ("p1", "p2", "p3"):
        restarted.restore_jobs(project_id, process_probe=lambda _pid: None)
        restarted.queue.require_publication(project_id)
    p3_revision_before = repositories.jobs.load("p3").revision
    original_publish = repositories.jobs._publish_prepared_unchecked

    def fail_p2_persistently(project_id, *args, **kwargs):
        if project_id == "p2":
            raise RuntimeError("p2 persistent publication failure")
        return original_publish(project_id, *args, **kwargs)

    monkeypatch.setattr(
        repositories.jobs,
        "_publish_prepared_unchecked",
        fail_p2_persistently,
    )

    with pytest.raises(RuntimeError, match="p2 persistent publication failure"):
        restarted.reap_adopted_jobs()

    assert repositories.jobs.load("p3").revision == p3_revision_before + 1
    assert "p3" not in restarted.queue.pending_publication_projects()
    assert "p2" in restarted.queue.pending_publication_projects()


def test_service_persists_process_unverified_after_cancel_termination_failure(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
        terminate=lambda: (_ for _ in ()).throw(RuntimeError("tree still alive")),
    )
    queue._process_alive = lambda _pid: True

    result = service.cancel_job("p1", claimed.job_id)

    stored = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == claimed.job_id
    )
    assert result.status == "cancelling"
    assert result.stage == "process_unverified"
    assert stored["status"] == "cancelling"
    assert stored["stage"] == "process_unverified"


def test_service_publishes_cancelling_before_blocking_tree_termination(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    termination_started = threading.Event()
    allow_termination = threading.Event()
    queue._process_probe = lambda _pid: {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }

    def terminate(_pid: int) -> None:
        termination_started.set()
        assert allow_termination.wait(5)

    queue._terminate_tree = terminate
    result: list[object] = []
    thread = threading.Thread(
        target=lambda: result.append(service.cancel_job("p1", claimed.job_id))
    )
    thread.start()
    assert termination_started.wait(5)
    stored_during_cancel = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == claimed.job_id
    )
    assert stored_during_cancel["status"] == "cancelling"
    allow_termination.set()
    thread.join(5)

    assert not thread.is_alive()
    assert result and result[0].status == "cancelled"
    stored_after_cancel = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == claimed.job_id
    )
    assert stored_after_cancel["status"] == "cancelled"


def test_cancel_publication_failure_rolls_back_reservation_and_preserves_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    terminated: list[str] = []
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
        terminate=lambda: terminated.append("controller"),
    )
    original_publish = repositories.jobs._publish_prepared_unchecked
    failures = 1

    def fail_once(*args, **kwargs):
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("deterministic jobs publication failure")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(repositories.jobs, "_publish_prepared_unchecked", fail_once)

    with pytest.raises(RuntimeError, match="deterministic jobs publication failure"):
        service.cancel_job("p1", claimed.job_id)

    assert queue.status(claimed.job_id) == "running"
    cancelled = service.cancel_job("p1", claimed.job_id)
    assert cancelled.status == "cancelled"
    assert terminated == ["controller"]


def test_restore_cancelling_terminates_outside_service_and_queue_locks(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    add_project(
        repositories,
        tmp_path,
        "p2",
        (clip("two", project_id="p2"),),
    )
    service.enqueue_trajectory_jobs("p1")
    service.enqueue_trajectory_jobs("p2")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.project_id == "p1"
    lease = claimed.attempts[-1]
    service.record_job_process(
        "p1",
        claimed.job_id,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt.log",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    queue.begin_cancel(claimed.job_id)
    service._publish_queue("p1")

    termination_started = threading.Event()
    allow_termination = threading.Event()

    def terminate(_pid: int) -> None:
        termination_started.set()
        assert allow_termination.wait(5)

    restarted = ProjectService(
        repositories,
        LocalResourceQueue(process_tree_terminator=terminate),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-03T00:00:02Z",
    )
    restarted.restore_jobs("p2", process_probe=lambda _pid: None)
    p2_job = next(
        item
        for item in restarted.queue.jobs()
        if item.project_id == "p2" and item.status == "running"
    )
    p2_claimed = restarted.queue.claim_next_unstarted()
    assert p2_claimed is not None and p2_claimed.job_id == p2_job.job_id
    p2_lease = p2_claimed.attempts[-1]
    restore_result: list[object] = []
    restore_thread = threading.Thread(
        target=lambda: restore_result.append(
            restarted.restore_jobs(
                "p1",
                process_probe=lambda _pid: {
                    "pid": 123,
                    "process_start_time": "start-1",
                    "command_fingerprint": "command-1",
                    "task_token": "token-1",
                },
            )
        )
    )
    restore_thread.start()
    assert termination_started.wait(5)
    callback_result: list[object] = []
    callback_thread = threading.Thread(
        target=lambda: callback_result.append(
            restarted.update_job_progress(
                "p2",
                p2_job.job_id,
                AdapterProgress(stage="still_running", message="worker heartbeat"),
                attempt_number=p2_lease.number,
                claim_token=str(p2_lease.worker_claim_token),
            )
        )
    )
    callback_thread.start()
    callback_thread.join(1)
    callback_completed_before_cleanup = not callback_thread.is_alive()
    allow_termination.set()
    restore_thread.join(5)
    callback_thread.join(5)

    assert callback_completed_before_cleanup
    assert callback_result and callback_result[0].stage == "still_running"
    assert restore_result


def test_job_actions_check_expected_revision_inside_service_state_guard(
    tmp_path: Path,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    job_id = queue.running_ids()[0]
    status_before = queue.status(job_id)
    current_revision = repositories.jobs.load("p1").revision

    with pytest.raises(RevisionConflict):
        service.cancel_job(
            "p1", job_id, expected_jobs_revision=current_revision - 1
        )

    assert queue.status(job_id) == status_before
