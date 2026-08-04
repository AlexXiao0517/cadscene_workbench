from __future__ import annotations

from dataclasses import replace
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


def test_merge_restored_empty_manifest_clears_only_explicit_target_project() -> None:
    queue = LocalResourceQueue()
    queue.submit(replace(job("p1-job"), project_id="p1"))
    queue.submit(replace(job("p2-job"), project_id="p2"))

    queue.merge_restored((), queue_order=(), project_id="p1")

    assert tuple(item.job_id for item in queue.jobs()) == ("p2-job",)


def test_merge_restored_rejects_job_from_another_target_project() -> None:
    queue = LocalResourceQueue()

    with pytest.raises(ValueError, match="target project"):
        queue.merge_restored(
            (replace(job("foreign"), project_id="p2"),),
            queue_order=("foreign",),
            project_id="p1",
        )


def test_idempotency_reuses_only_identical_input_and_adapter_identity() -> None:
    queue = LocalResourceQueue()
    first = queue.submit(job("a", idempotency_key="same"))
    repeated = queue.submit(job("b", idempotency_key="same"))
    changed = queue.submit(job("c", input_fingerprint="input-v2"))

    assert repeated.job_id == first.job_id
    assert changed.job_id == "c"


def test_successful_worker_cancel_releases_claim_and_late_finally_is_idempotent() -> (
    None
):
    queue = LocalResourceQueue()
    queue.submit(
        job("a").with_attempt(AttemptRecord(number=1, directory="jobs/a/attempt-1"))
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]

    queue.cancel("a")
    assert "a" not in queue._execution_claims
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


@pytest.mark.parametrize(
    "later_observation",
    (
        None,
        {
            "pid": 123,
            "process_start_time": "different-start",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
    ),
)
def test_adopted_process_probe_uncertainty_keeps_capacity_and_claim_fail_closed(
    later_observation,
) -> None:
    initial_observation = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    observed = [initial_observation]
    running = (
        job("a", exclusive_key="trajectory:clip-1")
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
        [running, job("b", exclusive_key="trajectory:clip-1")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: observed[0],
        process_alive=lambda _pid: True,
    )
    observed[0] = later_observation

    assert queue.poll_adopted_processes() == ()

    assert queue.status("a") in {"running", "cancelling"}
    assert queue.status("b") == "queued"
    assert queue.claim_next_unstarted() is None


def test_adopted_process_probe_error_is_not_proof_of_exit() -> None:
    initial = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    probe_available = [True]

    def probe(_pid: int):
        if not probe_available[0]:
            raise RuntimeError("process identity is inaccessible")
        return initial

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
        process_probe=probe,
        process_alive=lambda _pid: True,
    )
    probe_available[0] = False

    assert queue.poll_adopted_processes() == ()
    assert queue.status("b") == "queued"


def test_restore_identity_inspection_errors_retain_resource_fail_closed() -> None:
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
        process_probe=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("identity access denied")
        ),
        process_alive=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("liveness access denied")
        ),
    )

    assert queue.status("a") == "cancelling"
    assert queue.status("b") == "queued"


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


class _FakeWin32ProcessApi:
    def __init__(
        self,
        *,
        processes: tuple[tuple[int, int], ...] = (),
        open_handle: object | None = object(),
        last_error: int = 0,
        exit_code: int = 259,
    ) -> None:
        self.processes = processes
        self.open_handle = open_handle
        self.error = last_error
        self.exit_code = exit_code
        self.closed: list[object] = []

    def snapshot_processes(self) -> tuple[tuple[int, int], ...]:
        return self.processes

    def open_process(self, desired_access: int, pid: int) -> object | None:
        assert desired_access == 0x1000
        assert pid == 123
        return self.open_handle

    def get_exit_code(self, handle: object) -> int:
        assert handle is self.open_handle
        return self.exit_code

    def terminate_process(self, handle: object, exit_code: int) -> bool:
        raise AssertionError("liveness inspection must not terminate a process")

    def close_handle(self, handle: object) -> None:
        self.closed.append(handle)

    def get_last_error(self) -> int:
        return self.error


def test_native_windows_snapshot_finds_recursive_descendants_without_psutil() -> None:
    import cadscene.projects.queue as queue_module

    api = _FakeWin32ProcessApi(
        processes=((4, 0), (123, 4), (201, 123), (202, 201), (300, 4))
    )

    assert queue_module._descendant_pids(123, windows_api=api) == {201, 202}


def test_native_windows_liveness_access_denied_fails_closed() -> None:
    import cadscene.projects.queue as queue_module

    api = _FakeWin32ProcessApi(open_handle=None, last_error=5)

    with pytest.raises(PermissionError, match="access denied.*PID 123"):
        queue_module._pid_alive(123, windows_api=api)


def test_native_windows_liveness_invalid_parameter_proves_nonexistent() -> None:
    import cadscene.projects.queue as queue_module

    api = _FakeWin32ProcessApi(open_handle=None, last_error=87)

    assert queue_module._pid_alive(123, windows_api=api) is False
    assert api.closed == []


def test_native_windows_liveness_always_closes_open_handle() -> None:
    import cadscene.projects.queue as queue_module

    handle = object()
    api = _FakeWin32ProcessApi(open_handle=handle)

    assert queue_module._pid_alive(123, windows_api=api) is True
    assert api.closed == [handle]


def test_restore_blocks_capacity_when_changed_process_identity_is_unverified() -> None:
    active = (
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
    terminated: list[int] = []

    queue = LocalResourceQueue.restore(
        [active, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: None,
        process_alive=lambda _pid: True,
        process_tree_terminator=terminated.append,
        current_fingerprint_resolver=lambda item: (
            "changed" if item.job_id == "a" else item.input_fingerprint
        ),
    )

    assert queue.status("a") == "cancelling"
    assert "unverified" in (queue.get("a").error or "")
    assert queue.status("b") == "queued"
    assert terminated == []


def test_restore_cleanup_failure_keeps_resource_reserved_and_retry_blocked() -> None:
    active = (
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
        [active, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
        process_alive=lambda _pid: True,
        process_tree_terminator=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("tree still alive")
        ),
        current_fingerprint_resolver=lambda item: (
            "changed" if item.job_id == "a" else item.input_fingerprint
        ),
    )

    assert queue.status("a") == "cancelling"
    assert "tree still alive" in (queue.get("a").error or "")
    assert queue.status("b") == "queued"
    with pytest.raises(ValueError, match="terminal unsuccessful"):
        queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))


def test_changed_input_cleanup_target_survives_crash_between_restore_phases() -> None:
    identity = {
        "pid": 123,
        "process_start_time": "start-1",
        "command_fingerprint": "command-1",
        "task_token": "token-1",
    }
    active = (
        job("a")
        .with_attempt(
            AttemptRecord(
                number=1,
                directory="jobs/a/attempt-1",
                **identity,
            )
        )
        .with_status("running")
    )
    first_restore = LocalResourceQueue.restore(
        [active, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: identity,
        process_alive=lambda _pid: True,
        current_fingerprint_resolver=lambda item: (
            "changed-input" if item.job_id == "a" else item.input_fingerprint
        ),
        defer_cleanup=True,
    )
    persisted = [item.to_dict() for item in first_restore.jobs()]
    terminated: list[int] = []

    second_restore = LocalResourceQueue.restore(
        persisted,
        queue_order=first_restore.queue_order(),
        process_probe=lambda _pid: identity,
        process_alive=lambda _pid: True,
        process_tree_terminator=terminated.append,
    )

    assert terminated == [123]
    assert second_restore.status("a") == "superseded"
    assert second_restore.status("b") == "running"


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
    cancelling = queue.cancel("a")

    assert cancelling.status == "cancelling"
    assert cancelling.stage == "process_unverified"
    with pytest.raises(ValueError, match="terminal unsuccessful"):
        queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))


def test_cancel_termination_failure_retains_claim_controller_and_resource_slot() -> (
    None
):
    def fail_termination() -> None:
        raise RuntimeError("descendant remained alive")

    queue = LocalResourceQueue(process_alive=lambda _pid: True)
    queue.submit(
        job("a", exclusive_key="trajectory:clip-1").with_attempt(
            AttemptRecord(number=1, directory="jobs/a/attempt-1")
        )
    )
    queue.submit(job("b", exclusive_key="trajectory:clip-1"))
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == "a"
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
        terminate=fail_termination,
    )

    result = queue.cancel("a")

    assert result.status == "cancelling"
    assert result.stage == "process_unverified"
    assert queue.status("b") == "queued"
    assert queue.claim_next_unstarted() is None
    assert queue._execution_claims["a"] == (lease.number, lease.worker_claim_token)
    assert (
        "a",
        lease.number,
        str(lease.worker_claim_token),
    ) in queue._process_controllers


def test_cancel_can_retry_the_retained_controller_after_unverified_failure() -> None:
    calls = 0

    def terminate() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("tree still alive")

    process_alive = [True]
    queue = LocalResourceQueue(process_alive=lambda _pid: process_alive[0])
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
        terminate=terminate,
    )
    assert queue.cancel("a").status == "cancelling"
    process_alive[0] = False

    assert queue.cancel("a").status == "cancelled"
    assert calls == 2


def test_successful_cancel_releases_adopted_lease_before_retry() -> None:
    identity = {
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
                **identity,
            )
        )
        .with_status("running")
    )
    alive = [True]
    terminated: list[int] = []

    def terminate(pid: int) -> None:
        terminated.append(pid)
        alive[0] = False

    queue = LocalResourceQueue.restore(
        [running],
        queue_order=("a",),
        process_probe=lambda _pid: identity,
        process_alive=lambda _pid: alive[0],
        process_tree_terminator=terminate,
    )
    old_lease = queue._execution_claims["a"]

    assert queue.cancel("a").status == "cancelled"
    assert terminated == [123]
    assert "a" not in queue._execution_claims
    assert "a" not in queue._adopted_attempts

    retried = queue.retry("a", AttemptRecord(number=2, directory="jobs/a/attempt-2"))
    claimed = queue.claim_next_unstarted()

    assert retried.status == "running"
    assert claimed is not None
    new_lease = claimed.attempts[-1]
    assert (new_lease.number, new_lease.worker_claim_token) != old_lease


def test_failed_cancel_retains_adopted_lease_and_blocks_retry() -> None:
    identity = {
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
                **identity,
            )
        )
        .with_status("running")
    )
    queue = LocalResourceQueue.restore(
        [running],
        queue_order=("a",),
        process_probe=lambda _pid: identity,
        process_alive=lambda _pid: True,
        process_tree_terminator=lambda _pid: (_ for _ in ()).throw(
            RuntimeError("tree still alive")
        ),
    )
    old_lease = queue._execution_claims["a"]

    cancelled = queue.cancel("a")

    assert cancelled.status == "cancelling"
    assert queue._execution_claims["a"] == old_lease
    assert queue._adopted_attempts["a"] == old_lease
    with pytest.raises(ValueError, match="claim.*active"):
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


@pytest.mark.parametrize(
    ("restored_status", "expected_terminal"),
    (("running", "superseded"), ("cancelling", "interrupted")),
)
def test_restore_cleans_verified_changed_input_process_before_releasing_capacity(
    restored_status: str,
    expected_terminal: str,
) -> None:
    terminated: list[int] = []
    active = (
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
        .with_status(restored_status)
    )

    queue = LocalResourceQueue.restore(
        [active, job("b")],
        queue_order=("a", "b"),
        process_probe=lambda _pid: {
            "pid": 123,
            "process_start_time": "start-1",
            "command_fingerprint": "command-1",
            "task_token": "token-1",
        },
        current_fingerprint_resolver=lambda item: (
            "changed-input" if item.job_id == "a" else item.input_fingerprint
        ),
        process_tree_terminator=terminated.append,
    )

    assert terminated == [123]
    assert queue.status("a") == expected_terminal
    assert queue.status("b") == "running"


def test_success_candidate_is_invisible_until_explicit_commit(tmp_path: Path) -> None:
    queue = LocalResourceQueue()
    submitted = queue.submit(
        job("candidate", resource_class="light_compute").with_attempt(
            AttemptRecord(number=1, directory=str(tmp_path / "attempt-1"))
        )
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    attempt = claimed.attempts[-1]
    queue.mark_validating(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )

    candidate = queue.prepare_success_candidate(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
        output_revision="out-1",
        output_fingerprint="f" * 64,
        published_outputs={"result": str(tmp_path / "result")},
    )

    assert candidate.status == "success"
    assert queue.get(submitted.job_id).status == "validating"
    committed = queue.commit_prepared_candidate(
        submitted.job_id,
        candidate=replace(
            candidate,
            operation_id="publish-op",
            publication_operation_id="publish-op",
        ),
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )
    assert committed.status == "success"
    assert committed.operation_id == "publish-op"


@pytest.mark.parametrize(
    "pollution",
    [
        "missing_fingerprint",
        "unvalidated",
        "wrong_validated_input",
        "missing_publication_operation",
        "missing_outputs",
    ],
)
def test_prepared_success_candidate_rejects_structural_degradation(
    tmp_path: Path,
    pollution: str,
) -> None:
    queue = LocalResourceQueue()
    submitted = queue.submit(
        job("prepared", resource_class="light_compute").with_attempt(
            AttemptRecord(number=1, directory=str(tmp_path / "attempt-1"))
        )
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    attempt = claimed.attempts[-1]
    queue.mark_validating(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )
    candidate = queue.prepare_success_candidate(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
        output_revision="out-1",
        output_fingerprint="f" * 64,
        published_outputs={"result": str(tmp_path / "result.json")},
    )
    candidate = replace(
        candidate,
        operation_id="publish-operation",
        publication_operation_id="publish-operation",
    )
    if pollution == "missing_fingerprint":
        candidate = replace(candidate, output_fingerprint=None)
    elif pollution == "unvalidated":
        candidate = replace(
            candidate,
            output_validated=False,
            validated_input_fingerprint=None,
        )
    elif pollution == "wrong_validated_input":
        candidate = replace(candidate, validated_input_fingerprint="other-input")
    elif pollution == "missing_publication_operation":
        candidate = replace(candidate, publication_operation_id=None)
    else:
        candidate = replace(candidate, published_outputs={})

    with pytest.raises(ValueError, match="prepared success candidate"):
        queue.commit_prepared_candidate(
            submitted.job_id,
            candidate=candidate,
            attempt_number=attempt.number,
            claim_token=attempt.worker_claim_token,
        )
    assert queue.get(submitted.job_id).status == "validating"


@pytest.mark.parametrize(
    "pollution",
    ["clip_id", "job_type", "adapter", "stage", "output_validated"],
)
def test_recovered_terminal_candidate_rejects_contract_pollution(
    tmp_path: Path,
    pollution: str,
) -> None:
    queue = LocalResourceQueue()
    submitted = queue.submit(
        job("recovered", resource_class="media_io").with_attempt(
            AttemptRecord(number=1, directory=str(tmp_path / "attempt-1"))
        )
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    attempt = claimed.attempts[-1]
    queue.mark_validating(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )
    success = queue.prepare_success_candidate(
        submitted.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
        output_revision="out-1",
        output_fingerprint="f" * 64,
        published_outputs={"video": str(tmp_path / "rendered.mp4")},
        validation_proof={"video_sha256": "f" * 64},
    )
    candidate = replace(
        success,
        status="failed",
        stage="failed",
        operation_id="restore-operation",
        publication_operation_id="restore-operation",
        output_validated=False,
        validated_input_fingerprint=None,
        error="restore validation failed",
    )
    if pollution == "clip_id":
        candidate = replace(candidate, clip_id="other-clip")
    elif pollution == "job_type":
        candidate = replace(candidate, job_type="merge")
    elif pollution == "adapter":
        candidate = replace(candidate, adapter_name="other-adapter")
    elif pollution == "stage":
        candidate = replace(candidate, stage="success")
    else:
        candidate = replace(
            candidate,
            output_validated=True,
            validated_input_fingerprint=candidate.input_fingerprint,
        )

    with pytest.raises(ValueError, match="recovered terminal candidate"):
        queue.commit_recovered_terminal_candidate(
            submitted.job_id,
            candidate=candidate,
            attempt_number=attempt.number,
            claim_token=attempt.worker_claim_token,
        )
    assert queue.get(submitted.job_id).status == "validating"


def test_submission_candidates_do_not_mutate_queue_before_manifest_publish() -> None:
    queue = LocalResourceQueue()
    first = job("analysis-cad", resource_class="light_compute")
    second = job(
        "analysis-video",
        resource_class="light_compute",
        depends_on_job_ids=(first.job_id,),
    )

    batch = queue.prepare_submission_candidates((first, second))

    assert batch.job_ids == (
        "analysis-cad",
        "analysis-video",
    )
    assert batch.new_candidates == batch.jobs
    assert batch.reused_jobs == ()
    assert queue.jobs() == ()
    committed = queue.commit_submission_candidates(batch.new_candidates)
    assert tuple(item.job_id for item in committed) == (
        "analysis-cad",
        "analysis-video",
    )
    assert queue.status("analysis-cad") == "running"
    assert queue.status("analysis-video") == "queued"


def test_submission_batch_reuses_job_and_rebinds_new_dependency() -> None:
    queue = LocalResourceQueue()
    existing_cad = queue.submit(
        job(
            "existing-cad",
            resource_class="light_compute",
            idempotency_key="cad-key",
        )
    )
    proposed_cad = job(
        "proposed-cad",
        resource_class="light_compute",
        idempotency_key="cad-key",
    )
    proposed_video = job(
        "proposed-video",
        resource_class="light_compute",
        depends_on_job_ids=(proposed_cad.job_id,),
        idempotency_key="video-key",
    )

    batch = queue.prepare_submission_candidates(
        (proposed_cad, proposed_video)
    )

    assert batch.reused_jobs == (existing_cad,)
    assert batch.new_candidates[0].job_id == proposed_video.job_id
    assert batch.new_candidates[0].depends_on_job_ids == (existing_cad.job_id,)
    assert batch.job_ids == (existing_cad.job_id, proposed_video.job_id)
    committed = queue.commit_submission_candidates(batch.new_candidates)
    assert tuple(item.job_id for item in committed) == (proposed_video.job_id,)
