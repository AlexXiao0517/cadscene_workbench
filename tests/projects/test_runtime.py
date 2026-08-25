from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import sys
import threading
import time

import pytest

from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.adapters import AdapterResult
from cadscene.projects.executor import JobExecutionPlan, LocalJobExecutor
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.runtime import ProjectRootLease, ProjectRuntime
from cadscene.projects.service import ProjectService
from cadscene.projects.workflow_adapters import default_workflow_adapters
from tests.projects.test_service_jobs import clip, service_with_clips


def test_project_root_lease_prevents_two_serve_viewers_from_owning_same_root(
    tmp_path: Path,
) -> None:
    first = ProjectRootLease(tmp_path / "projects")
    second = ProjectRootLease(tmp_path / "projects")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_runtime_restores_persisted_jobs_before_starting_worker(
    tmp_path: Path,
) -> None:
    _old_service, repositories, old_queue = service_with_clips(
        tmp_path, (clip("one"),)
    )
    _old_service.enqueue_trajectory_jobs("p1")
    persisted_active = set(old_queue.running_ids())
    assert persisted_active

    restarted_queue = LocalResourceQueue()
    restarted_service = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "later",
    )
    worker_called = threading.Event()

    class RecordingExecutor:
        def run_next(self):
            worker_called.set()
            return None

    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=restarted_service,
        executor=RecordingExecutor(),
        analysis=None,
        poll_interval=0.01,
    )
    runtime.start()
    try:
        assert worker_called.wait(1)
        restored = repositories.jobs.load("p1")
        by_id = {item["job_id"]: item for item in restored.jobs}
        assert all(by_id[job_id]["status"] == "interrupted" for job_id in persisted_active)
        assert set(restarted_queue.queue_order()) == set(by_id)
    finally:
        runtime.close()


def test_runtime_worker_keeps_polling_until_orderly_shutdown(tmp_path: Path) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "later",
    )
    calls = 0
    enough = threading.Event()

    class RecordingExecutor:
        def run_next(self):
            nonlocal calls
            calls += 1
            if calls >= 2:
                enough.set()
            return None

    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=service,
        executor=RecordingExecutor(),
        analysis=None,
        poll_interval=0.01,
    )
    runtime.start()
    assert enough.wait(1)
    runtime.close()
    stopped_calls = calls
    time.sleep(0.05)

    assert calls == stopped_calls


def test_runtime_runs_multiple_resource_slots_without_serializing_all_jobs(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "later",
    )
    release = threading.Event()
    two_workers_entered = threading.Event()
    lock = threading.Lock()
    active_calls = 0

    class BlockingExecutor:
        def run_next(self):
            nonlocal active_calls
            with lock:
                active_calls += 1
                if active_calls >= 2:
                    two_workers_entered.set()
            release.wait(1)
            with lock:
                active_calls -= 1
            return None

    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=service,
        executor=BlockingExecutor(),
        analysis=None,
        poll_interval=0.01,
    )
    runtime.start()
    try:
        assert two_workers_entered.wait(0.5), (
            "independent queue resource slots were serialized by one runtime worker"
        )
    finally:
        release.set()
        runtime.close()


def test_runtime_marks_alive_but_unverifiable_old_process_interrupted(
    tmp_path: Path,
) -> None:
    old_service, repositories, old_queue = service_with_clips(
        tmp_path, (clip("one"),)
    )
    old_service.enqueue_trajectory_jobs("p1")
    job_id = old_queue.running_ids()[0]
    jobs = repositories.jobs.load("p1")

    def make_unverifiable(value):
        changed = []
        for item in value.jobs:
            if item["job_id"] != job_id:
                changed.append(item)
                continue
            attempts = [dict(attempt) for attempt in item["attempts"]]
            attempts[-1].update(
                {
                    "pid": os.getpid(),
                    "process_start_time": "unverifiable",
                    "command_fingerprint": "bad",
                    "task_token": "old",
                }
            )
            changed.append({**item, "status": "running", "stage": "running", "attempts": attempts})
        return replace(value, jobs=tuple(changed))

    repositories.jobs.update(
        "p1", expected_revision=jobs.revision, mutate=make_unverifiable
    )
    restarted_queue = LocalResourceQueue(process_alive=lambda pid: pid == os.getpid())
    service = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "later",
    )

    class IdleExecutor:
        def run_next(self):
            return None

    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=service,
        executor=IdleExecutor(),
        analysis=None,
        poll_interval=0.01,
    )
    runtime.start()
    try:
        assert restarted_queue.status(job_id) == "interrupted"
    finally:
        runtime.close()


def test_runtime_close_cancels_claimed_work_instead_of_leaving_worker_hung(
    tmp_path: Path,
) -> None:
    old_service, repositories, old_queue = service_with_clips(
        tmp_path, (clip("one"),)
    )
    old_service.enqueue_trajectory_jobs("p1")
    job_id = old_queue.running_ids()[0]
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "later",
    )
    started = threading.Event()

    class BlockingExecutor:
        def run_next(self):
            job = queue.claim_next_unstarted()
            if job is None:
                return None
            started.set()
            while queue.status(job.job_id) not in {
                "cancelled",
                "interrupted",
                "failed",
                "success",
                "stale_input",
                "superseded",
            }:
                time.sleep(0.005)
            return queue.get(job.job_id)

    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=service,
        executor=BlockingExecutor(),
        analysis=None,
        poll_interval=0.005,
    )
    runtime.start()
    service.retry_job("p1", job_id)
    assert started.wait(1)
    close_error: Exception | None = None
    try:
        runtime.close(timeout=0.1)
    except Exception as exc:
        close_error = exc
    finally:
        if runtime._thread is not None and runtime._thread.is_alive():
            service.cancel_job("p1", job_id)
            runtime.close(timeout=1)

    assert close_error is None
    assert queue.status(job_id) == "cancelled"


def test_runtime_real_executor_consumes_jobs_enqueued_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    marker = tmp_path / "executed.txt"
    plan = JobExecutionPlan(
        commands=(
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ok', encoding='utf-8')",
            ),
        ),
        validate=lambda: AdapterResult.success(
            output_revision="runtime-output",
            output_fingerprint="runtime-fingerprint",
            outputs={"marker": str(marker)},
        ),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )
    runtime = ProjectRuntime(
        projects_root=tmp_path / "projects",
        repositories=repositories,
        service=service,
        executor=LocalJobExecutor(service),
        analysis=None,
        poll_interval=0.005,
    )
    runtime.start()
    try:
        service.enqueue_trajectory_jobs("p1")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            jobs = repositories.jobs.load("p1").jobs
            if jobs and all(item["status"] == "success" for item in jobs):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("runtime did not execute the durable queue")
        assert marker.read_text(encoding="utf-8") == "ok"
    finally:
        runtime.close()
