from __future__ import annotations

from pathlib import Path
import threading

import pytest

from cadscene.projects.adapters import AdapterProgress
from cadscene.projects.queue import AttemptRecord, LocalResourceQueue, QueueJob


def job(
    job_id: str,
    *,
    resource_class: str = "heavy_compute",
    depends_on_job_ids: tuple[str, ...] = (),
    exclusive_key: str | None = None,
    input_fingerprint: str = "input-v1",
    idempotency_key: str | None = None,
) -> QueueJob:
    return QueueJob(
        job_id=job_id,
        project_id="project-1",
        clip_id="clip-1",
        job_type="trajectory",
        resource_class=resource_class,
        status="queued",
        stage="queued",
        priority=0,
        depends_on_job_ids=depends_on_job_ids,
        exclusive_key=exclusive_key,
        idempotency_key=idempotency_key or f"key-{job_id}-{input_fingerprint}",
        input_revision="analysis-1",
        input_fingerprint=input_fingerprint,
        adapter_name="sfm_only",
        adapter_version="1",
        output_revision=None,
        operation_id=f"operation-{job_id}",
        attempts=(),
    )


def test_heavy_jobs_run_one_at_a_time_by_default() -> None:
    queue = LocalResourceQueue()

    queue.submit(job("a"))
    queue.submit(job("b"))

    assert queue.running_ids() == ["a"]
    assert queue.status("b") == "queued"


def test_each_static_resource_class_has_an_independent_default_capacity() -> None:
    queue = LocalResourceQueue()

    for name, resource in (
        ("heavy", "heavy_compute"),
        ("light", "light_compute"),
        ("media", "media_io"),
        ("control", "control"),
    ):
        queue.submit(job(name, resource_class=resource))

    assert queue.running_ids() == ["heavy", "light", "media", "control"]


def test_dependency_must_be_validated_success_for_current_output() -> None:
    queue = LocalResourceQueue()
    queue.submit(
        job("export", resource_class="media_io").with_attempt(
            AttemptRecord(number=1, directory="jobs/export/attempt-1")
        )
    )
    queue.submit(job("solve", depends_on_job_ids=("export",)))

    assert queue.status("solve") == "queued"

    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    queue.mark_validating(
        "export", attempt_number=lease.number, claim_token=lease.worker_claim_token
    )
    queue.mark_success(
        "export",
        output_revision="out-1",
        output_validated=False,
        attempt_number=lease.number,
        claim_token=lease.worker_claim_token,
    )
    assert queue.status("solve") == "queued"

    queue.validate_output("export", current_input_fingerprint="input-v1")
    assert queue.status("solve") == "running"


def test_exclusive_key_prevents_concurrent_duplicate_solves() -> None:
    queue = LocalResourceQueue(capacities={"heavy_compute": 2})
    queue.submit(job("a", exclusive_key="trajectory:clip-1"))
    queue.submit(job("b", exclusive_key="trajectory:clip-1"))

    assert queue.running_ids() == ["a"]
    assert queue.status("b") == "queued"


def test_progress_never_invents_a_fraction() -> None:
    progress = AdapterProgress(stage="feature_matching", message="matching frames")

    assert progress.fraction is None
    assert progress.to_dict() == {
        "stage": "feature_matching",
        "message": "matching frames",
    }
    with pytest.raises(ValueError, match="fraction"):
        AdapterProgress(stage="bad", message="bad", fraction=1.1)


def test_queue_persists_stage_only_progress_without_fabricated_fraction() -> None:
    queue = LocalResourceQueue()
    queue.submit(
        job("a").with_attempt(AttemptRecord(number=1, directory="jobs/a/attempt-1"))
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]

    updated = queue.update_progress(
        "a",
        AdapterProgress(stage="matching", message="matching frames"),
        attempt_number=lease.number,
        claim_token=lease.worker_claim_token,
    )

    assert updated.stage == "matching"
    assert updated.progress == {"stage": "matching", "message": "matching frames"}


def test_cancel_terminates_process_tree_and_preserves_attempt_directory(
    tmp_path: Path,
) -> None:
    terminated: list[int] = []
    attempt_dir = tmp_path / "jobs/a/attempt-1"
    attempt_dir.mkdir(parents=True)
    log = attempt_dir / "adapter.log"
    log.write_text("partial output", encoding="utf-8")
    running = job("a").with_attempt(
        AttemptRecord(
            number=1,
            directory=str(attempt_dir),
            pid=123,
            process_start_time="start-1",
            command_fingerprint="command-1",
            task_token="token-1",
        )
    )
    queue = LocalResourceQueue(
        process_tree_terminator=terminated.append,
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
    )
    queue.submit(running)

    queue.cancel("a")

    assert terminated == [123]
    assert queue.status("a") == "cancelled"
    assert log.read_text(encoding="utf-8") == "partial output"


def test_cancel_that_cannot_terminate_tree_becomes_interrupted() -> None:
    def fail_termination(_pid: int) -> None:
        raise RuntimeError("descendant remained alive")

    running = job("a").with_attempt(
        AttemptRecord(
            number=1,
            directory="jobs/a/attempt-1",
            pid=123,
            process_start_time="start-1",
            command_fingerprint="command-1",
            task_token="token-1",
        )
    )
    queue = LocalResourceQueue(
        process_tree_terminator=fail_termination,
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
    )
    queue.submit(running)

    cancelled = queue.cancel("a")

    assert cancelled.status == "interrupted"
    assert cancelled.error == "descendant remained alive"


def test_cancel_refuses_pid_fallback_when_full_process_identity_does_not_match() -> (
    None
):
    terminated: list[int] = []
    running = job("a").with_attempt(
        AttemptRecord(
            number=1,
            directory="jobs/a/attempt-1",
            pid=123,
            process_start_time="start-1",
            command_fingerprint="command-1",
            task_token="token-1",
        )
    )
    queue = LocalResourceQueue(
        process_tree_terminator=terminated.append,
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "another-task",
        },
    )
    queue.submit(running)

    cancelled = queue.cancel("a")

    assert cancelled.status == "interrupted"
    assert "identity" in (cancelled.error or "")
    assert terminated == []


def test_retry_releases_old_controller_and_uses_only_new_attempt_controller() -> None:
    calls: list[str] = []
    queue = LocalResourceQueue()
    queue.submit(
        job("a").with_attempt(AttemptRecord(number=1, directory="jobs/a/attempt-1"))
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    first_lease = claimed.attempts[-1]
    queue.record_process(
        "a",
        attempt_number=first_lease.number,
        claim_token=first_lease.worker_claim_token,
        pid=101,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt-1.log",
    )
    queue.register_process_controller(
        "a",
        lambda: calls.append("old"),
        attempt_number=first_lease.number,
        claim_token=first_lease.worker_claim_token,
    )
    queue.mark_failed(
        "a",
        "failed",
        attempt_number=first_lease.number,
        claim_token=first_lease.worker_claim_token,
    )
    queue.release_process_controller(
        "a",
        101,
        attempt_number=first_lease.number,
        claim_token=first_lease.worker_claim_token,
    )
    queue.release_execution_claim(
        "a",
        attempt_number=first_lease.number,
        claim_token=first_lease.worker_claim_token,
    )
    queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    second_lease = claimed.attempts[-1]
    queue.record_process(
        "a",
        attempt_number=second_lease.number,
        claim_token=second_lease.worker_claim_token,
        pid=202,
        process_start_time="start-2",
        command_fingerprint="command-2",
        task_token="token-2",
        log_path="attempt-2.log",
    )
    queue.register_process_controller(
        "a",
        lambda: calls.append("new"),
        attempt_number=second_lease.number,
        claim_token=second_lease.worker_claim_token,
    )

    queue.cancel("a")

    assert calls == ["new"]


@pytest.mark.parametrize(
    "probe, expected",
    [
        (
            {
                "pid": 123,
                "process_start_time": "start-1",
                "command_fingerprint": "command-1",
                "task_token": "token-1",
            },
            "running",
        ),
        (
            {
                "pid": 123,
                "process_start_time": "different",
                "command_fingerprint": "command-1",
                "task_token": "token-1",
            },
            "interrupted",
        ),
    ],
)
def test_restart_adopts_only_fully_verified_process_identity(probe, expected) -> None:
    attempt = AttemptRecord(
        number=1,
        directory="jobs/a/attempt-1",
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
    )
    running = job("a").with_attempt(attempt).with_status("running", stage="running")

    queue = LocalResourceQueue.restore(
        [running],
        queue_order=["a"],
        process_probe=lambda _pid: probe,
    )

    assert queue.status("a") == expected


def test_restart_rejects_process_with_incomplete_persisted_identity() -> None:
    attempt = AttemptRecord(
        number=1,
        directory="jobs/a/attempt-1",
        pid=123,
        process_start_time="start-1",
        command_fingerprint=None,
        task_token=None,
    )
    running = job("a").with_attempt(attempt).with_status("running", stage="running")

    queue = LocalResourceQueue.restore(
        [running],
        queue_order=["a"],
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": None,
            "task_token": None,
        },
    )

    assert queue.status("a") == "interrupted"


def test_restore_preserves_persistent_queued_order() -> None:
    queue = LocalResourceQueue.restore(
        [job("b"), job("a")],
        queue_order=["a", "b"],
    )

    assert queue.running_ids() == ["a"]
    assert queue.status("b") == "queued"


def test_idempotency_reuses_only_identical_input_and_adapter_identity() -> None:
    queue = LocalResourceQueue()
    first = queue.submit(job("a", idempotency_key="same"))
    repeated = queue.submit(job("b", idempotency_key="same"))
    changed = queue.submit(job("c", input_fingerprint="input-v2"))

    assert repeated.job_id == first.job_id
    assert changed.job_id == "c"


def test_cancel_blocks_retry_until_old_attempt_claim_is_released() -> None:
    queue = LocalResourceQueue()
    queue.submit(
        job("a").with_attempt(AttemptRecord(number=1, directory="jobs/a/attempt-1"))
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]

    queue.cancel("a")

    with pytest.raises(ValueError, match="claim.*active"):
        queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))
    queue.release_execution_claim(
        "a", attempt_number=lease.number, claim_token=lease.worker_claim_token
    )
    retried = queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))
    new_claim = queue.claim_next_unstarted()
    assert new_claim is not None
    new_lease = new_claim.attempts[-1]

    with pytest.raises(ValueError, match="attempt lease"):
        queue.update_progress(
            "a",
            AdapterProgress(stage="late", message="old worker callback"),
            attempt_number=lease.number,
            claim_token=lease.worker_claim_token,
        )
    with pytest.raises(ValueError, match="attempt lease"):
        queue.release_execution_claim(
            "a",
            attempt_number=lease.number,
            claim_token=lease.worker_claim_token,
        )
    with pytest.raises(ValueError, match="attempt lease"):
        queue.mark_validating(
            "a",
            attempt_number=lease.number,
            claim_token=lease.worker_claim_token,
        )
    updated = queue.update_progress(
        "a",
        AdapterProgress(stage="new", message="new worker"),
        attempt_number=new_lease.number,
        claim_token=new_lease.worker_claim_token,
    )
    assert retried.attempts[-1].number == 2
    assert updated.stage == "new"


def test_cancel_reserves_state_before_blocking_process_termination() -> None:
    termination_started = threading.Event()
    allow_termination = threading.Event()

    def terminate(_pid: int) -> None:
        termination_started.set()
        assert allow_termination.wait(5)

    queue = LocalResourceQueue(
        process_tree_terminator=terminate,
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
    )
    queue.submit(
        job("a").with_attempt(AttemptRecord(number=1, directory="jobs/a/attempt-1"))
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]
    queue.record_process(
        "a",
        attempt_number=lease.number,
        claim_token=lease.worker_claim_token,
        pid=123,
        process_start_time="start-1",
        command_fingerprint="command-1",
        task_token="token-1",
        log_path="attempt-1.log",
    )

    thread = threading.Thread(target=lambda: queue.cancel("a"))
    thread.start()
    assert termination_started.wait(5)
    assert queue.status("a") == "cancelling"
    with pytest.raises(ValueError, match="active attempt"):
        queue.update_progress(
            "a",
            AdapterProgress(stage="late", message="late"),
            attempt_number=lease.number,
            claim_token=lease.worker_claim_token,
        )
    allow_termination.set()
    thread.join(5)
    assert not thread.is_alive()
    assert queue.status("a") == "cancelled"


def test_verified_adopted_process_is_reaped_when_it_exits() -> None:
    observed = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    running = (
        job("a")
        .with_attempt(
            AttemptRecord(
                number=1,
                directory="jobs/a/attempt-1",
                pid=123,
                process_start_time="start-1",
                command_fingerprint="command-1",
                task_token="token-1",
            )
        )
        .with_status("running")
    )
    queue = LocalResourceQueue.restore(
        [running, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: observed or None,
    )
    assert queue.status("b") == "queued"

    observed.clear()
    reaped = queue.poll_adopted_processes()

    assert reaped == ("a",)
    assert queue.status("a") == "interrupted"
    assert queue.status("b") == "running"


def test_windows_fallback_rediscovers_children_before_terminating_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cadscene.projects.queue as queue_module

    discoveries = iter(({201}, {202}, set(), set(), set(), set()))
    alive = {123, 201, 202}
    terminated: list[int] = []

    def descendants(_pid: int) -> set[int]:
        return next(discoveries, set())

    def terminate(pid: int) -> None:
        terminated.append(pid)
        alive.discard(pid)

    queue_module._terminate_windows_process_tree(
        123,
        timeout=1,
        descendant_resolver=descendants,
        alive=lambda pid: pid in alive,
        terminate=terminate,
        sleep=lambda _seconds: None,
    )

    assert terminated == [201, 202, 123]


def test_retry_refuses_unverified_old_process_after_cancel_failure() -> None:
    queue = LocalResourceQueue(
        process_tree_terminator=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("descendant remained alive")
        ),
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
        process_alive=lambda _pid: True,
    )
    queue.submit(
        job("a").with_attempt(
            AttemptRecord(
                number=1,
                directory="jobs/a/attempt-1",
                pid=123,
                process_start_time="start-1",
                command_fingerprint="command-1",
                task_token="token-1",
            )
        )
    )
    interrupted = queue.cancel("a")

    assert interrupted.status == "interrupted"
    with pytest.raises(ValueError, match="old process.*not proven gone"):
        queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))


def test_restore_resumes_verified_cancelling_cleanup_as_interrupted() -> None:
    terminated: list[int] = []
    cancelling = (
        job("a")
        .with_attempt(
            AttemptRecord(
                number=1,
                directory="jobs/a/attempt-1",
                pid=123,
                process_start_time="start-1",
                command_fingerprint="command-1",
                task_token="token-1",
            )
        )
        .with_status("cancelling")
    )

    queue = LocalResourceQueue.restore(
        [cancelling, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
        process_tree_terminator=terminated.append,
    )

    assert terminated == [123]
    assert queue.status("a") == "interrupted"
    assert queue.status("b") == "running"
