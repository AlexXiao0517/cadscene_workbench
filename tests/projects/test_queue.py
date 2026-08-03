from __future__ import annotations

from pathlib import Path

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
    queue.submit(job("export", resource_class="media_io"))
    queue.submit(job("solve", depends_on_job_ids=("export",)))

    assert queue.status("solve") == "queued"

    queue.mark_validating("export")
    queue.mark_success("export", output_revision="out-1", output_validated=False)
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
    queue.submit(job("a"))

    updated = queue.update_progress(
        "a", AdapterProgress(stage="matching", message="matching frames")
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
    queue = LocalResourceQueue(process_tree_terminator=terminated.append)
    queue.submit(running)

    queue.cancel("a")

    assert terminated == [123]
    assert queue.status("a") == "cancelled"
    assert log.read_text(encoding="utf-8") == "partial output"


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
