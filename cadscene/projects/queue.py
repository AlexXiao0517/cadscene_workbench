from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import signal
import threading
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from uuid import uuid4

from .adapters import AdapterProgress


RESOURCE_CLASSES = ("heavy_compute", "light_compute", "media_io", "control")
ACTIVE_STATUSES = {"preparing", "running", "validating"}
RESOURCE_HOLDING_STATUSES = ACTIVE_STATUSES | {"cancelling"}
TERMINAL_STATUSES = {
    "success",
    "failed",
    "interrupted",
    "cancelled",
    "superseded",
    "stale_input",
}


@dataclass(frozen=True)
class AttemptRecord:
    number: int
    directory: str
    pid: int | None = None
    process_start_time: str | None = None
    command_fingerprint: str | None = None
    task_token: str | None = None
    log_path: str | None = None
    worker_claim_token: str | None = None

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValueError("attempt number must be positive")
        if not self.directory:
            raise ValueError("attempt directory must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "directory": self.directory,
            "pid": self.pid,
            "process_start_time": self.process_start_time,
            "command_fingerprint": self.command_fingerprint,
            "task_token": self.task_token,
            "log_path": self.log_path,
            "worker_claim_token": self.worker_claim_token,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AttemptRecord:
        return cls(
            number=int(value["number"]),
            directory=str(value["directory"]),
            pid=None if value.get("pid") is None else int(value["pid"]),
            process_start_time=_optional_string(value.get("process_start_time")),
            command_fingerprint=_optional_string(value.get("command_fingerprint")),
            task_token=_optional_string(value.get("task_token")),
            log_path=_optional_string(value.get("log_path")),
            worker_claim_token=_optional_string(value.get("worker_claim_token")),
        )


@dataclass(frozen=True)
class CancelReservation:
    job_id: str
    attempt_number: int | None
    claim_token: str | None
    pid: int | None
    process_start_time: str | None
    command_fingerprint: str | None
    task_token: str | None
    controller: Callable[[], None] | None
    previous_job: QueueJob


@dataclass(frozen=True)
class RestoreCleanupReservation:
    job_id: str
    attempt_number: int
    pid: int
    process_start_time: str
    command_fingerprint: str
    task_token: str
    target_job: QueueJob


@dataclass(frozen=True)
class QueueJob:
    job_id: str
    project_id: str
    clip_id: str
    job_type: str
    resource_class: str
    status: str
    stage: str
    priority: int
    depends_on_job_ids: tuple[str, ...]
    exclusive_key: str | None
    idempotency_key: str
    input_revision: str
    input_fingerprint: str
    adapter_name: str
    adapter_version: str
    output_revision: str | None
    operation_id: str
    attempts: tuple[AttemptRecord, ...]
    submission_operation_id: str | None = None
    publication_operation_id: str | None = None
    output_fingerprint: str | None = None
    output_validated: bool = False
    validated_input_fingerprint: str | None = None
    published_outputs: Mapping[str, str] = field(default_factory=dict)
    validation_proof: Mapping[str, object] | None = None
    progress: Mapping[str, object] | None = None
    error: str | None = None
    cleanup_reason: str | None = None
    target_terminal_status: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "job_id",
            "project_id",
            "clip_id",
            "job_type",
            "status",
            "stage",
            "idempotency_key",
            "input_revision",
            "input_fingerprint",
            "adapter_name",
            "adapter_version",
            "operation_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.resource_class not in RESOURCE_CLASSES:
            raise ValueError(f"unknown resource class: {self.resource_class}")
        if self.job_id in self.depends_on_job_ids:
            raise ValueError("a job cannot depend on itself")
        if (
            self.target_terminal_status is not None
            and self.target_terminal_status not in TERMINAL_STATUSES
        ):
            raise ValueError("cleanup target must be a terminal status")
        numbers = [attempt.number for attempt in self.attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("attempt numbers must be immutable and contiguous")

    def with_status(self, status: str, *, stage: str | None = None) -> QueueJob:
        return replace(self, status=status, stage=stage or status)

    def with_attempt(self, attempt: AttemptRecord) -> QueueJob:
        if attempt.number != len(self.attempts) + 1:
            raise ValueError("new attempts must append without replacing history")
        return replace(self, attempts=(*self.attempts, attempt))

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "job_type": self.job_type,
            "resource_class": self.resource_class,
            "status": self.status,
            "stage": self.stage,
            "priority": self.priority,
            "depends_on_job_ids": list(self.depends_on_job_ids),
            "exclusive_key": self.exclusive_key,
            "idempotency_key": self.idempotency_key,
            "input_revision": self.input_revision,
            "input_fingerprint": self.input_fingerprint,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "output_revision": self.output_revision,
            "output_fingerprint": self.output_fingerprint,
            "output_validated": self.output_validated,
            "validated_input_fingerprint": self.validated_input_fingerprint,
            "operation_id": self.operation_id,
            "submission_operation_id": self.submission_operation_id,
            "publication_operation_id": self.publication_operation_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "published_outputs": dict(self.published_outputs),
            "validation_proof": (
                None if self.validation_proof is None else dict(self.validation_proof)
            ),
            "progress": None if self.progress is None else dict(self.progress),
            "error": self.error,
            "cleanup_reason": self.cleanup_reason,
            "target_terminal_status": self.target_terminal_status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> QueueJob:
        return cls(
            job_id=str(value["job_id"]),
            project_id=str(value["project_id"]),
            clip_id=str(value["clip_id"]),
            job_type=str(value["job_type"]),
            resource_class=str(value["resource_class"]),
            status=str(value["status"]),
            stage=str(value["stage"]),
            priority=int(value.get("priority", 0)),
            depends_on_job_ids=tuple(
                str(item) for item in value.get("depends_on_job_ids", ())
            ),
            exclusive_key=_optional_string(value.get("exclusive_key")),
            idempotency_key=str(value["idempotency_key"]),
            input_revision=str(value["input_revision"]),
            input_fingerprint=str(value["input_fingerprint"]),
            adapter_name=str(value["adapter_name"]),
            adapter_version=str(value["adapter_version"]),
            output_revision=_optional_string(value.get("output_revision")),
            operation_id=str(value["operation_id"]),
            submission_operation_id=_optional_string(
                value.get("submission_operation_id")
            ),
            publication_operation_id=_optional_string(
                value.get("publication_operation_id")
            ),
            attempts=tuple(
                AttemptRecord.from_dict(item)  # type: ignore[arg-type]
                for item in value.get("attempts", ())
            ),
            output_fingerprint=_optional_string(value.get("output_fingerprint")),
            output_validated=bool(value.get("output_validated", False)),
            validated_input_fingerprint=_optional_string(
                value.get("validated_input_fingerprint")
            ),
            published_outputs={
                str(key): str(item)
                for key, item in dict(value.get("published_outputs", {})).items()  # type: ignore[arg-type]
            },
            validation_proof=(
                None
                if value.get("validation_proof") is None
                else dict(value["validation_proof"])  # type: ignore[arg-type]
            ),
            progress=(
                None
                if value.get("progress") is None
                else dict(value["progress"])  # type: ignore[arg-type]
            ),
            error=_optional_string(value.get("error")),
            cleanup_reason=_optional_string(value.get("cleanup_reason")),
            target_terminal_status=_optional_string(
                value.get("target_terminal_status")
            ),
        )


@dataclass(frozen=True)
class PreparedSubmissionBatch:
    jobs: tuple[QueueJob, ...]
    new_candidates: tuple[QueueJob, ...]
    reused_jobs: tuple[QueueJob, ...]

    @property
    def job_ids(self) -> tuple[str, ...]:
        return tuple(item.job_id for item in self.jobs)

    @property
    def new_job_ids(self) -> frozenset[str]:
        return frozenset(item.job_id for item in self.new_candidates)


class TaskQueue(Protocol):
    def submit(self, job: QueueJob) -> QueueJob:
        ...

    def status(self, job_id: str) -> str:
        ...

    def running_ids(self) -> list[str]:
        ...

    def cancel(self, job_id: str) -> QueueJob:
        ...


class _Win32ProcessApi(Protocol):
    def snapshot_processes(self) -> tuple[tuple[int, int], ...]:
        ...

    def open_process(self, desired_access: int, pid: int) -> object | None:
        ...

    def get_exit_code(self, handle: object) -> int:
        ...

    def terminate_process(self, handle: object, exit_code: int) -> bool:
        ...

    def close_handle(self, handle: object) -> None:
        ...

    def get_last_error(self) -> int:
        ...


class LocalResourceQueue:
    def __init__(
        self,
        *,
        capacities: Mapping[str, int] | None = None,
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        process_tree_terminator: Callable[[int], None] | None = None,
        process_alive: Callable[[int], bool] | None = None,
        claim_token_factory: Callable[[], str] | None = None,
    ) -> None:
        configured = {name: 1 for name in RESOURCE_CLASSES}
        configured.update(capacities or {})
        if set(configured) != set(RESOURCE_CLASSES):
            raise ValueError("capacities must use only static resource classes")
        if any(
            isinstance(value, bool) or int(value) < 1 for value in configured.values()
        ):
            raise ValueError("resource capacities must be positive integers")
        self.capacities = {name: int(value) for name, value in configured.items()}
        self._jobs: dict[str, QueueJob] = {}
        self._queue_order: list[str] = []
        self._terminate_tree = process_tree_terminator or terminate_process_tree
        self._process_probe = process_probe or probe_process_identity
        self._process_alive = process_alive or _pid_alive
        self._claim_token_factory = claim_token_factory or (lambda: uuid4().hex)
        self._process_controllers: dict[
            tuple[str, int, str], tuple[int, Callable[[], None]]
        ] = {}
        self._execution_claims: dict[str, tuple[int, str]] = {}
        self._adopted_attempts: dict[str, tuple[int, str]] = {}
        self._restore_cleanups: dict[str, RestoreCleanupReservation] = {}
        self._cancel_reservations: set[str] = set()
        self._publication_gated = False
        self._pending_publication_projects: set[str] = set()
        self._lock = threading.RLock()

    @property
    def process_lock(self) -> threading.RLock:
        return self._lock

    def execution_claimed_ids(self) -> tuple[str, ...]:
        """Return jobs currently owned by a worker attempt."""

        with self._lock:
            return tuple(
                job_id for job_id in self._queue_order if job_id in self._execution_claims
            )

    @classmethod
    def restore(
        cls,
        jobs: Iterable[QueueJob | Mapping[str, object]],
        *,
        queue_order: Sequence[str],
        capacities: Mapping[str, int] | None = None,
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        current_fingerprint_resolver: Callable[[QueueJob], str | None] | None = None,
        process_tree_terminator: Callable[[int], None] | None = None,
        process_alive: Callable[[int], bool] | None = None,
        schedule: bool = True,
        defer_cleanup: bool = False,
        unverified_process_policy: str = "fail_closed",
    ) -> LocalResourceQueue:
        if unverified_process_policy not in {"fail_closed", "interrupt"}:
            raise ValueError("unsupported unverified process policy")
        queue = cls(
            capacities=capacities,
            process_probe=process_probe,
            process_tree_terminator=process_tree_terminator,
            process_alive=process_alive,
        )
        records = [
            item if isinstance(item, QueueJob) else QueueJob.from_dict(item)
            for item in jobs
        ]
        queue._jobs = {item.job_id: item for item in records}
        if set(queue_order) != set(queue._jobs):
            raise ValueError("queue_order must contain every restored job exactly once")
        if len(queue_order) != len(set(queue_order)):
            raise ValueError("queue_order must not contain duplicates")
        queue._queue_order = list(queue_order)
        invalidated: dict[str, QueueJob] = {}
        if current_fingerprint_resolver is not None:
            for job_id in queue._queue_order:
                current = queue._jobs[job_id]
                fingerprint = current_fingerprint_resolver(current)
                if fingerprint != current.input_fingerprint:
                    invalidated[job_id] = _invalidated_job(current)
            changed = True
            while changed:
                changed = False
                for job_id in queue._queue_order:
                    current = queue._jobs[job_id]
                    if job_id in invalidated:
                        continue
                    if any(
                        dependency in invalidated
                        for dependency in current.depends_on_job_ids
                    ):
                        invalidated[job_id] = _invalidated_job(current)
                        changed = True
            for job_id, replacement in invalidated.items():
                if queue._jobs[job_id].status not in RESOURCE_HOLDING_STATUSES:
                    queue._jobs[job_id] = replacement
        probe = process_probe or (lambda _pid: None)
        for job_id in queue._queue_order:
            current = queue._jobs[job_id]
            if current.status not in RESOURCE_HOLDING_STATUSES:
                continue
            cleanup_target = None
            cleanup_reason = current.cleanup_reason
            if current.status == "cancelling" and current.target_terminal_status:
                cleanup_target = _cleanup_terminal_job(
                    current, current.target_terminal_status
                )
            elif current.status == "cancelling":
                cleanup_target = _cleanup_terminal_job(current, "interrupted")
                cleanup_reason = "cancellation_interrupted_by_restart"
            elif job_id in invalidated:
                cleanup_target = invalidated[job_id]
                cleanup_reason = "input_changed"
            attempt = current.attempts[-1] if current.attempts else None
            if attempt is None or attempt.pid is None:
                queue._jobs[job_id] = (
                    cleanup_target
                    if cleanup_target is not None
                    else _cleanup_terminal_job(current, "interrupted")
                )
                continue
            observed = _probe_or_unverified(probe, attempt.pid)
            expected = {
                "pid": attempt.pid,
                "process_start_time": attempt.process_start_time,
                "command_fingerprint": attempt.command_fingerprint,
                "task_token": attempt.task_token,
            }
            identity_matches = not (
                any(value in (None, "") for value in expected.values())
                or observed is None
                or any(observed.get(key) != value for key, value in expected.items())
            )
            if cleanup_target is not None:
                if identity_matches:
                    reservation = RestoreCleanupReservation(
                        job_id=job_id,
                        attempt_number=attempt.number,
                        pid=attempt.pid,
                        process_start_time=str(attempt.process_start_time),
                        command_fingerprint=str(attempt.command_fingerprint),
                        task_token=str(attempt.task_token),
                        target_job=cleanup_target,
                    )
                    queue._restore_cleanups[job_id] = reservation
                    queue._jobs[job_id] = replace(
                        current.with_status("cancelling"),
                        error="verified old process cleanup is pending",
                        cleanup_reason=cleanup_reason,
                        target_terminal_status=cleanup_target.status,
                    )
                elif unverified_process_policy == "interrupt":
                    queue._jobs[job_id] = cleanup_target
                elif not _process_may_be_alive(queue._process_alive, attempt.pid):
                    queue._jobs[job_id] = cleanup_target
                else:
                    queue._jobs[job_id] = replace(
                        current.with_status("cancelling"),
                        error="process identity is unverified; recovery is blocked",
                        cleanup_reason=cleanup_reason,
                        target_terminal_status=cleanup_target.status,
                    )
            elif not identity_matches:
                if unverified_process_policy == "interrupt":
                    queue._jobs[job_id] = current.with_status("interrupted")
                elif _process_may_be_alive(queue._process_alive, attempt.pid):
                    queue._jobs[job_id] = replace(
                        current.with_status("cancelling"),
                        error="process identity is unverified; recovery is blocked",
                    )
                else:
                    queue._jobs[job_id] = current.with_status("interrupted")
            else:
                claim_token = current.attempts[-1].worker_claim_token or uuid4().hex
                if current.attempts[-1].worker_claim_token is None:
                    adopted_attempt = replace(
                        current.attempts[-1], worker_claim_token=claim_token
                    )
                    current = replace(
                        current,
                        attempts=(*current.attempts[:-1], adopted_attempt),
                    )
                    queue._jobs[job_id] = current
                lease = (current.attempts[-1].number, claim_token)
                queue._execution_claims[job_id] = lease
                queue._adopted_attempts[job_id] = lease
        if not defer_cleanup:
            for reservation in tuple(queue._restore_cleanups.values()):
                cleanup_error: Exception | None = None
                try:
                    queue.terminate_restore_cleanup(reservation)
                except Exception as exc:
                    cleanup_error = exc
                queue.complete_restore_cleanup(reservation, error=cleanup_error)
        if schedule:
            queue._schedule()
        return queue

    def merge_restored(
        self,
        jobs: Iterable[QueueJob | Mapping[str, object]],
        *,
        project_id: str,
        queue_order: Sequence[str],
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        current_fingerprint_resolver: Callable[[QueueJob], str | None] | None = None,
        defer_cleanup: bool = False,
        unverified_process_policy: str = "fail_closed",
    ) -> LocalResourceQueue:
        incoming_values = [
            item if isinstance(item, QueueJob) else QueueJob.from_dict(item)
            for item in jobs
        ]
        incoming_projects = {item.project_id for item in incoming_values}
        if incoming_projects - {project_id}:
            raise ValueError("restored job does not belong to target project")
        restored = LocalResourceQueue.restore(
            incoming_values,
            queue_order=queue_order,
            capacities=self.capacities,
            process_probe=process_probe,
            current_fingerprint_resolver=current_fingerprint_resolver,
            process_tree_terminator=self._terminate_tree,
            process_alive=self._process_alive,
            schedule=False,
            defer_cleanup=defer_cleanup,
            unverified_process_policy=unverified_process_policy,
        )
        with self._lock:
            retained_order = [
                job_id
                for job_id in self._queue_order
                if self._jobs[job_id].project_id != project_id
            ]
            retained_jobs = {job_id: self._jobs[job_id] for job_id in retained_order}
            retained_controllers = {
                lease: record
                for lease, record in self._process_controllers.items()
                if lease[0] in retained_jobs
            }
            retained_claims = {
                job_id: lease
                for job_id, lease in self._execution_claims.items()
                if job_id in retained_jobs
            }
            retained_adopted = {
                job_id: lease
                for job_id, lease in self._adopted_attempts.items()
                if job_id in retained_jobs
            }
            self._jobs = {**retained_jobs, **restored._jobs}
            self._queue_order = [*retained_order, *restored._queue_order]
            self._process_controllers = retained_controllers
            self._execution_claims = {
                **retained_claims,
                **restored._execution_claims,
            }
            self._adopted_attempts = {
                **retained_adopted,
                **restored._adopted_attempts,
            }
            self._restore_cleanups = {
                job_id: reservation
                for job_id, reservation in self._restore_cleanups.items()
                if job_id in retained_jobs
            }
            self._restore_cleanups.update(restored._restore_cleanups)
            if process_probe is not None:
                self._process_probe = process_probe
            self._schedule_locked()
            return self

    def pending_restore_cleanups(
        self, project_id: str | None = None
    ) -> tuple[RestoreCleanupReservation, ...]:
        with self._lock:
            return tuple(
                reservation
                for job_id, reservation in self._restore_cleanups.items()
                if project_id is None or self._jobs[job_id].project_id == project_id
            )

    def terminate_restore_cleanup(self, reservation: RestoreCleanupReservation) -> None:
        observed = _probe_or_unverified(self._process_probe, reservation.pid)
        expected = {
            "pid": reservation.pid,
            "process_start_time": reservation.process_start_time,
            "command_fingerprint": reservation.command_fingerprint,
            "task_token": reservation.task_token,
        }
        if observed is None or any(
            observed.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeError(
                "refusing recovery cleanup because process identity is no longer verified"
            )
        self._terminate_tree(reservation.pid)

    def complete_restore_cleanup(
        self,
        reservation: RestoreCleanupReservation,
        *,
        error: Exception | None,
    ) -> QueueJob:
        with self._lock:
            current_reservation = self._restore_cleanups.get(reservation.job_id)
            current = self._jobs[reservation.job_id]
            attempt = current.attempts[-1] if current.attempts else None
            if (
                current_reservation != reservation
                or current.status != "cancelling"
                or attempt is None
                or attempt.number != reservation.attempt_number
                or attempt.pid != reservation.pid
            ):
                raise ValueError("restore cleanup reservation lost its attempt lease")
            if error is None:
                self._jobs[reservation.job_id] = reservation.target_job
                self._restore_cleanups.pop(reservation.job_id, None)
                self._schedule_locked()
            else:
                self._jobs[reservation.job_id] = replace(
                    current,
                    error=f"old process cleanup is unverified: {error}",
                )
            return self._jobs[reservation.job_id]

    def enable_publication_gate(self) -> None:
        """Prevent workers from claiming scheduled state until its owner is durable."""
        with self._lock:
            self._publication_gated = True
            self._pending_publication_projects.update(
                job.project_id
                for job in self._jobs.values()
                if job.status in ACTIVE_STATUSES
                and (not job.attempts or job.attempts[-1].pid is None)
            )

    def pending_publication_projects(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pending_publication_projects))

    def require_publication(self, project_id: str) -> None:
        with self._lock:
            if self._publication_gated:
                self._pending_publication_projects.add(project_id)

    def acknowledge_publication(self, project_id: str) -> None:
        with self._lock:
            self._pending_publication_projects.discard(project_id)

    def submit(self, job: QueueJob) -> QueueJob:
        with self._lock:
            for existing in self._jobs.values():
                if existing.idempotency_key == job.idempotency_key:
                    if (
                        existing.input_fingerprint != job.input_fingerprint
                        or existing.adapter_name != job.adapter_name
                        or existing.adapter_version != job.adapter_version
                    ):
                        raise ValueError(
                            "idempotency key collision across input or adapter identity"
                        )
                    return existing
            if job.job_id in self._jobs:
                raise ValueError(f"duplicate job_id: {job.job_id}")
            missing = set(job.depends_on_job_ids) - set(self._jobs)
            if missing:
                raise ValueError(f"unknown dependencies: {sorted(missing)}")
            self._jobs[job.job_id] = job
            self._queue_order.append(job.job_id)
            self._schedule_locked()
            return self._jobs[job.job_id]

    def prepare_submission_candidates(
        self, jobs: Sequence[QueueJob]
    ) -> PreparedSubmissionBatch:
        """Validate a batch without mutating queue state.

        The caller may durably publish these exact candidates before committing
        them to the process-local scheduler.
        """

        with self._lock:
            known = dict(self._jobs)
            prepared: list[QueueJob] = []
            new_candidates: list[QueueJob] = []
            reused_jobs: list[QueueJob] = []
            dependency_aliases: dict[str, str] = {}
            for job in jobs:
                normalized = replace(
                    job,
                    depends_on_job_ids=tuple(
                        dependency_aliases.get(item, item)
                        for item in job.depends_on_job_ids
                    ),
                )
                existing = next(
                    (
                        item
                        for item in known.values()
                        if item.idempotency_key == normalized.idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    if (
                        existing.input_fingerprint != job.input_fingerprint
                        or existing.adapter_name != job.adapter_name
                        or existing.adapter_version != job.adapter_version
                    ):
                        raise ValueError(
                            "idempotency key collision across input or adapter identity"
                        )
                    dependency_aliases[job.job_id] = existing.job_id
                    prepared.append(existing)
                    reused_jobs.append(existing)
                    continue
                if normalized.job_id in known:
                    raise ValueError(f"duplicate job_id: {normalized.job_id}")
                missing = set(normalized.depends_on_job_ids) - set(known)
                if missing:
                    raise ValueError(f"unknown dependencies: {sorted(missing)}")
                known[normalized.job_id] = normalized
                dependency_aliases[job.job_id] = normalized.job_id
                prepared.append(normalized)
                new_candidates.append(normalized)
            return PreparedSubmissionBatch(
                jobs=tuple(prepared),
                new_candidates=tuple(new_candidates),
                reused_jobs=tuple(reused_jobs),
            )

    def commit_submission_candidates(
        self, candidates: Sequence[QueueJob]
    ) -> tuple[QueueJob, ...]:
        """CAS-like in-memory commit after the jobs manifest is durable."""

        with self._lock:
            committed: list[QueueJob] = []
            for candidate in candidates:
                existing = next(
                    (
                        item
                        for item in self._jobs.values()
                        if item.idempotency_key == candidate.idempotency_key
                    ),
                    None,
                )
                if existing is not None:
                    if existing.to_dict() != candidate.to_dict():
                        raise ValueError("durable submission candidate changed before commit")
                    committed.append(existing)
                    continue
                missing = set(candidate.depends_on_job_ids) - set(self._jobs)
                if missing:
                    raise ValueError(f"unknown dependencies: {sorted(missing)}")
                if candidate.job_id in self._jobs:
                    raise ValueError(f"duplicate job_id: {candidate.job_id}")
                self._jobs[candidate.job_id] = candidate
                self._queue_order.append(candidate.job_id)
                committed.append(candidate)
            self._schedule_locked()
            return tuple(self._jobs[item.job_id] for item in committed)

    def status(self, job_id: str) -> str:
        with self._lock:
            return self._jobs[job_id].status

    def get(self, job_id: str) -> QueueJob:
        with self._lock:
            return self._jobs[job_id]

    def jobs(self) -> tuple[QueueJob, ...]:
        with self._lock:
            return tuple(self._jobs[job_id] for job_id in self._queue_order)

    def queue_order(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._queue_order)

    def running_ids(self) -> list[str]:
        with self._lock:
            return [
                job_id
                for job_id in self._queue_order
                if self._jobs[job_id].status in ACTIVE_STATUSES
            ]

    def claim_next_unstarted(self) -> QueueJob | None:
        """Atomically claim one queue-reserved attempt for a local worker."""
        with self._lock:
            for job_id in self._queue_order:
                current = self._jobs[job_id]
                attempt = current.attempts[-1] if current.attempts else None
                if (
                    current.status in ACTIVE_STATUSES
                    and current.project_id not in self._pending_publication_projects
                    and attempt is not None
                    and attempt.pid is None
                    and job_id not in self._execution_claims
                ):
                    token = self._claim_token_factory()
                    claimed_attempt = replace(attempt, worker_claim_token=token)
                    claimed = replace(
                        current,
                        attempts=(*current.attempts[:-1], claimed_attempt),
                    )
                    self._jobs[job_id] = claimed
                    self._execution_claims[job_id] = (claimed_attempt.number, token)
                    return claimed
        return None

    def release_execution_claim(
        self, job_id: str, *, attempt_number: int, claim_token: str | None
    ) -> None:
        with self._lock:
            if (
                job_id not in self._execution_claims
                and self._jobs[job_id].status in TERMINAL_STATUSES
            ):
                return
            self._require_claim_locked(job_id, attempt_number, claim_token)
            self._execution_claims.pop(job_id, None)
            self._adopted_attempts.pop(job_id, None)

    def mark_validating(
        self, job_id: str, *, attempt_number: int, claim_token: str | None
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            if current.status != "running":
                raise ValueError("only running jobs can begin validation")
            self._jobs[job_id] = current.with_status("validating")
            return self._jobs[job_id]

    def update_progress(
        self,
        job_id: str,
        progress: AdapterProgress,
        *,
        attempt_number: int,
        claim_token: str | None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            self._jobs[job_id] = replace(
                current,
                stage=progress.stage,
                progress=progress.to_dict(),
            )
            return self._jobs[job_id]

    def record_process(
        self,
        job_id: str,
        *,
        attempt_number: int,
        claim_token: str | None,
        pid: int,
        process_start_time: str,
        command_fingerprint: str,
        task_token: str,
        log_path: str,
        terminate: Callable[[], None] | None = None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            previous = current.attempts[-1]
            if previous.pid is not None:
                raise ValueError("attempt process identity is immutable once recorded")
            attempt = replace(
                previous,
                pid=pid,
                process_start_time=process_start_time,
                command_fingerprint=command_fingerprint,
                task_token=task_token,
                log_path=log_path,
            )
            self._jobs[job_id] = replace(
                current,
                attempts=(*current.attempts[:-1], attempt),
                status="running",
                stage="running",
            )
            if terminate is not None:
                self._process_controllers[
                    (job_id, attempt_number, str(claim_token))
                ] = (
                    pid,
                    terminate,
                )
            return self._jobs[job_id]

    def register_process_controller(
        self,
        job_id: str,
        terminate: Callable[[], None],
        *,
        attempt_number: int,
        claim_token: str | None,
    ) -> None:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            attempt = current.attempts[-1] if current.attempts else None
            if attempt is None or attempt.pid is None:
                raise ValueError(
                    "process controller requires recorded process identity"
                )
            self._process_controllers[(job_id, attempt_number, str(claim_token))] = (
                attempt.pid,
                terminate,
            )

    def release_process_controller(
        self,
        job_id: str,
        pid: int,
        *,
        attempt_number: int,
        claim_token: str | None,
    ) -> None:
        """Forget a completed attempt's in-memory cancellation callback."""
        with self._lock:
            lease = self._execution_claims.get(job_id)
            if lease != (attempt_number, claim_token):
                return
            record = self._process_controllers.get(
                (job_id, attempt_number, str(claim_token))
            )
            if record is not None and record[0] == pid:
                self._process_controllers.pop(
                    (job_id, attempt_number, str(claim_token)), None
                )

    def mark_success(
        self,
        job_id: str,
        *,
        attempt_number: int,
        claim_token: str | None,
        output_revision: str,
        output_fingerprint: str | None = None,
        output_validated: bool = True,
        published_outputs: Mapping[str, str] | None = None,
        validation_proof: Mapping[str, object] | None = None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            if current.status != "validating":
                raise ValueError("only validating jobs can succeed")
            self._jobs[job_id] = replace(
                current,
                status="success",
                stage="success",
                output_revision=output_revision,
                output_fingerprint=output_fingerprint,
                output_validated=output_validated,
                validated_input_fingerprint=(
                    current.input_fingerprint if output_validated else None
                ),
                published_outputs=dict(published_outputs or {}),
                validation_proof=(
                    None if validation_proof is None else dict(validation_proof)
                ),
                error=None,
            )
            self._schedule_locked()
            return self._jobs[job_id]

    def prepare_success_candidate(
        self,
        job_id: str,
        *,
        attempt_number: int,
        claim_token: str | None,
        output_revision: str,
        output_fingerprint: str | None = None,
        output_validated: bool = True,
        published_outputs: Mapping[str, str] | None = None,
        validation_proof: Mapping[str, object] | None = None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            if current.status != "validating":
                raise ValueError("only validating jobs can prepare success")
            return replace(
                current,
                status="success",
                stage="success",
                output_revision=output_revision,
                output_fingerprint=output_fingerprint,
                output_validated=output_validated,
                validated_input_fingerprint=(
                    current.input_fingerprint if output_validated else None
                ),
                published_outputs=dict(published_outputs or {}),
                validation_proof=(
                    None if validation_proof is None else dict(validation_proof)
                ),
                error=None,
            )

    def commit_prepared_candidate(
        self,
        job_id: str,
        *,
        candidate: QueueJob,
        attempt_number: int,
        claim_token: str | None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            if current.status != "validating":
                raise ValueError("only validating jobs can commit success")
            if (
                candidate.job_id != current.job_id
                or candidate.project_id != current.project_id
                or candidate.input_fingerprint != current.input_fingerprint
                or candidate.status != "success"
                or candidate.attempts != current.attempts
            ):
                raise ValueError("prepared success candidate no longer matches job lease")
            self._jobs[job_id] = candidate
            self._schedule_locked()
            return candidate

    def validate_output(
        self, job_id: str, *, current_input_fingerprint: str
    ) -> QueueJob:
        with self._lock:
            current = self._jobs[job_id]
            if current.status != "success":
                raise ValueError("only successful output can be validated")
            if current.input_fingerprint != current_input_fingerprint:
                raise ValueError("output does not match current input fingerprint")
            self._jobs[job_id] = replace(
                current,
                output_validated=True,
                validated_input_fingerprint=current_input_fingerprint,
            )
            self._schedule_locked()
            return self._jobs[job_id]

    def mark_failed(
        self,
        job_id: str,
        error: str,
        *,
        attempt_number: int,
        claim_token: str | None,
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            self._jobs[job_id] = replace(
                current.with_status("failed"), error=error, output_validated=False
            )
            self._schedule_locked()
            return self._jobs[job_id]

    def mark_stale_input(
        self, job_id: str, *, attempt_number: int, claim_token: str | None
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            self._jobs[job_id] = replace(
                current.with_status("stale_input"),
                output_revision=None,
                output_fingerprint=None,
                output_validated=False,
                validated_input_fingerprint=None,
                published_outputs={},
                validation_proof=None,
                error=None,
            )
            self._schedule_locked()
            return self._jobs[job_id]

    def mark_superseded(
        self, job_id: str, *, attempt_number: int, claim_token: str | None
    ) -> QueueJob:
        with self._lock:
            current = self._require_active_claim_locked(
                job_id, attempt_number, claim_token
            )
            self._jobs[job_id] = replace(
                current.with_status("superseded"),
                output_revision=None,
                output_fingerprint=None,
                output_validated=False,
                validated_input_fingerprint=None,
                published_outputs={},
                validation_proof=None,
                error=None,
            )
            self._schedule_locked()
            return self._jobs[job_id]

    def cancel(self, job_id: str) -> QueueJob:
        reservation = self.begin_cancel(job_id)
        if reservation is None:
            return self.get(job_id)
        error: Exception | None = None
        try:
            self.terminate_cancel_reservation(reservation)
        except Exception as exc:
            error = exc
        return self.complete_cancel(reservation, error=error)

    def begin_cancel(self, job_id: str) -> CancelReservation | None:
        with self._lock:
            current = self._jobs[job_id]
            if current.status in TERMINAL_STATUSES:
                return None
            if job_id in self._cancel_reservations:
                raise ValueError("job cancellation is already in progress")
            attempt = current.attempts[-1] if current.attempts else None
            number = None if attempt is None else attempt.number
            claim = self._execution_claims.get(job_id)
            claim_token = None if claim is None else claim[1]
            controller_record = (
                None
                if number is None or claim_token is None
                else self._process_controllers.get((job_id, number, str(claim_token)))
            )
            self._jobs[job_id] = replace(
                current.with_status("cancelling"),
                cleanup_reason="user_cancel",
                target_terminal_status="interrupted",
            )
            self._cancel_reservations.add(job_id)
            return CancelReservation(
                job_id=job_id,
                attempt_number=number,
                claim_token=claim_token,
                pid=None if attempt is None else attempt.pid,
                process_start_time=None
                if attempt is None
                else attempt.process_start_time,
                command_fingerprint=None
                if attempt is None
                else attempt.command_fingerprint,
                task_token=None if attempt is None else attempt.task_token,
                controller=None if controller_record is None else controller_record[1],
                previous_job=current,
            )

    def abort_cancel(self, reservation: CancelReservation) -> QueueJob:
        with self._lock:
            current = self._jobs[reservation.job_id]
            attempt = current.attempts[-1] if current.attempts else None
            if (
                current.status != "cancelling"
                or (None if attempt is None else attempt.number)
                != reservation.attempt_number
            ):
                raise ValueError("cancel reservation no longer owns the active attempt")
            self._jobs[reservation.job_id] = reservation.previous_job
            self._cancel_reservations.discard(reservation.job_id)
            return reservation.previous_job

    def terminate_cancel_reservation(self, reservation: CancelReservation) -> None:
        if reservation.pid is None:
            return
        if reservation.controller is not None:
            reservation.controller()
            return
        observed = _probe_or_unverified(self._process_probe, reservation.pid)
        expected = {
            "pid": reservation.pid,
            "process_start_time": reservation.process_start_time,
            "command_fingerprint": reservation.command_fingerprint,
            "task_token": reservation.task_token,
        }
        if (
            any(value in (None, "") for value in expected.values())
            or observed is None
            or any(observed.get(key) != value for key, value in expected.items())
        ):
            raise RuntimeError(
                "refusing PID fallback because process identity could not be verified"
            )
        self._terminate_tree(reservation.pid)

    def complete_cancel(
        self, reservation: CancelReservation, *, error: Exception | None
    ) -> QueueJob:
        with self._lock:
            current = self._jobs[reservation.job_id]
            attempt = current.attempts[-1] if current.attempts else None
            if (
                current.status != "cancelling"
                or (None if attempt is None else attempt.number)
                != reservation.attempt_number
            ):
                raise ValueError("cancel reservation no longer owns the active attempt")
            process_may_still_be_alive = False
            if error is not None and reservation.pid is not None:
                process_may_still_be_alive = _process_may_be_alive(
                    self._process_alive, reservation.pid
                )
            if process_may_still_be_alive:
                self._jobs[reservation.job_id] = replace(
                    current,
                    stage="process_unverified",
                    error=str(error),
                    output_revision=None,
                    output_fingerprint=None,
                    output_validated=False,
                    validated_input_fingerprint=None,
                    published_outputs={},
                    validation_proof=None,
                )
                self._cancel_reservations.discard(reservation.job_id)
                return self._jobs[reservation.job_id]
            status = "interrupted" if error is not None else "cancelled"
            self._jobs[reservation.job_id] = replace(
                current.with_status(status),
                error=None if error is None else str(error),
                output_revision=None,
                output_fingerprint=None,
                output_validated=False,
                validated_input_fingerprint=None,
                published_outputs={},
                validation_proof=None,
                cleanup_reason=None,
                target_terminal_status=None,
            )
            if reservation.attempt_number is not None and reservation.claim_token:
                lease = (
                    reservation.attempt_number,
                    str(reservation.claim_token),
                )
                self._process_controllers.pop(
                    (
                        reservation.job_id,
                        reservation.attempt_number,
                        str(reservation.claim_token),
                    ),
                    None,
                )
                if self._execution_claims.get(reservation.job_id) == lease:
                    self._execution_claims.pop(reservation.job_id, None)
                if self._adopted_attempts.get(reservation.job_id) == lease:
                    self._adopted_attempts.pop(reservation.job_id, None)
            self._cancel_reservations.discard(reservation.job_id)
            self._schedule_locked()
            return self._jobs[reservation.job_id]

    def retry(self, job_id: str, attempt: AttemptRecord) -> QueueJob:
        with self._lock:
            current = self._jobs[job_id]
            if job_id in self._execution_claims:
                raise ValueError("old attempt claim is still active")
            if current.status not in {
                "failed",
                "interrupted",
                "cancelled",
                "stale_input",
                "superseded",
            }:
                raise ValueError("only terminal unsuccessful jobs can be retried")
            previous = current.attempts[-1] if current.attempts else None
            if (
                previous is not None
                and previous.pid is not None
                and _process_may_be_alive(self._process_alive, previous.pid)
            ):
                raise ValueError("old process is not proven gone; retry is blocked")
            retried = replace(
                current.with_attempt(attempt),
                status="queued",
                stage="queued",
                output_revision=None,
                output_fingerprint=None,
                output_validated=False,
                validated_input_fingerprint=None,
                published_outputs={},
                validation_proof=None,
                error=None,
                cleanup_reason=None,
                target_terminal_status=None,
            )
            self._jobs[job_id] = retried
            self._schedule_locked()
            return self._jobs[job_id]

    def _require_claim_locked(
        self, job_id: str, attempt_number: int, claim_token: str | None
    ) -> QueueJob:
        if not claim_token or self._execution_claims.get(job_id) != (
            attempt_number,
            claim_token,
        ):
            raise ValueError("attempt lease does not match the current worker claim")
        current = self._jobs[job_id]
        attempt = current.attempts[-1] if current.attempts else None
        if (
            attempt is None
            or attempt.number != attempt_number
            or attempt.worker_claim_token != claim_token
        ):
            raise ValueError("attempt lease does not match the current attempt")
        return current

    def _require_active_claim_locked(
        self, job_id: str, attempt_number: int, claim_token: str | None
    ) -> QueueJob:
        current = self._require_claim_locked(job_id, attempt_number, claim_token)
        if current.status not in ACTIVE_STATUSES:
            raise ValueError("transition requires an active attempt")
        return current

    def _schedule(self) -> None:
        with self._lock:
            self._schedule_locked()

    def _schedule_locked(self) -> None:
        usage = {name: 0 for name in RESOURCE_CLASSES}
        exclusive = set()
        for current in self._jobs.values():
            if current.status in RESOURCE_HOLDING_STATUSES:
                usage[current.resource_class] += 1
                if current.exclusive_key is not None:
                    exclusive.add(current.exclusive_key)
        candidates = sorted(
            enumerate(self._queue_order),
            key=lambda item: (-self._jobs[item[1]].priority, item[0]),
        )
        for _, job_id in candidates:
            current = self._jobs[job_id]
            if current.status != "queued":
                continue
            if usage[current.resource_class] >= self.capacities[current.resource_class]:
                continue
            if current.exclusive_key is not None and current.exclusive_key in exclusive:
                continue
            if not self._dependencies_valid(current):
                continue
            preparing = current.with_status("preparing")
            running = preparing.with_status("running")
            self._jobs[job_id] = running
            if self._publication_gated:
                self._pending_publication_projects.add(current.project_id)
            usage[current.resource_class] += 1
            if current.exclusive_key is not None:
                exclusive.add(current.exclusive_key)

    def poll_adopted_processes(self) -> tuple[str, ...]:
        with self._lock:
            snapshots = tuple(
                (job_id, lease, self._jobs[job_id])
                for job_id, lease in self._adopted_attempts.items()
            )
        exited: list[tuple[str, int, str]] = []
        for job_id, (attempt_number, claim_token), current in snapshots:
            attempt = current.attempts[-1]
            observed = (
                None
                if attempt.pid is None
                else _probe_or_unverified(self._process_probe, attempt.pid)
            )
            expected = {
                "pid": attempt.pid,
                "process_start_time": attempt.process_start_time,
                "command_fingerprint": attempt.command_fingerprint,
                "task_token": attempt.task_token,
            }
            identity_is_uncertain = observed is None or any(
                observed.get(key) != value for key, value in expected.items()
            )
            if identity_is_uncertain:
                process_is_alive = _process_may_be_alive(
                    self._process_alive, attempt.pid
                )
                if not process_is_alive:
                    exited.append((job_id, attempt_number, claim_token))
        reaped: list[str] = []
        with self._lock:
            for job_id, attempt_number, claim_token in exited:
                if self._adopted_attempts.get(job_id) != (
                    attempt_number,
                    claim_token,
                ):
                    continue
                current = self._jobs[job_id]
                if current.status in ACTIVE_STATUSES:
                    self._jobs[job_id] = replace(
                        current.with_status("interrupted"),
                        error="adopted process exited without a validated completion sidecar",
                        output_revision=None,
                        output_fingerprint=None,
                        output_validated=False,
                        validated_input_fingerprint=None,
                        published_outputs={},
                        validation_proof=None,
                    )
                self._adopted_attempts.pop(job_id, None)
                self._execution_claims.pop(job_id, None)
                reaped.append(job_id)
            if reaped:
                self._schedule_locked()
        return tuple(reaped)

    def _dependencies_valid(self, job: QueueJob) -> bool:
        for dependency_id in job.depends_on_job_ids:
            dependency = self._jobs[dependency_id]
            if dependency.status != "success" or not dependency.output_validated:
                return False
            if dependency.validated_input_fingerprint != dependency.input_fingerprint:
                return False
        return True


def terminate_process_tree(pid: int, *, timeout: float = 5.0) -> None:
    if pid <= 0:
        raise ValueError("pid must be positive")
    if os.name == "nt":
        _terminate_windows_process_tree(pid, timeout=timeout)
        return
    descendants = _descendant_pids(pid)
    targets = {pid, *descendants}
    try:
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return
    os.killpg(process_group, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_pid_alive(item) for item in targets):
        time.sleep(0.02)
    remaining = {item for item in targets if _pid_alive(item)}
    if remaining and os.name != "nt":
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for item in remaining:
            try:
                os.kill(item, signal.SIGKILL)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(
            _pid_alive(item) for item in remaining
        ):
            time.sleep(0.02)
        remaining = {item for item in remaining if _pid_alive(item)}
    if remaining:
        raise RuntimeError(
            f"process tree did not terminate; remaining PIDs: {sorted(remaining)}"
        )


def _terminate_windows_process_tree(
    pid: int,
    *,
    timeout: float,
    descendant_resolver: Callable[[int], set[int]] | None = None,
    alive: Callable[[int], bool] | None = None,
    terminate: Callable[[int], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Fallback tree cancellation with repeated discovery and quiescence proof."""
    windows_api = (
        None
        if descendant_resolver is not None
        and alive is not None
        and terminate is not None
        else _CtypesWin32ProcessApi()
    )
    descendants = descendant_resolver or (
        lambda process_id: _descendant_pids(process_id, windows_api=windows_api)
    )
    is_alive = alive or (
        lambda process_id: _pid_alive(process_id, windows_api=windows_api)
    )
    terminate_one = terminate or (
        lambda process_id: _terminate_windows_process(
            process_id, windows_api=windows_api
        )
    )
    pause = sleep or time.sleep
    deadline = time.monotonic() + timeout
    known: set[int] = set()
    quiet_descendant_scans = 0
    while time.monotonic() < deadline and is_alive(pid):
        discovered = descendants(pid)
        known.update(discovered)
        live_children = {item for item in discovered if is_alive(item)}
        for child in sorted(live_children):
            terminate_one(child)
        rediscovered = {item for item in descendants(pid) if is_alive(item)}
        known.update(rediscovered)
        for child in sorted(rediscovered):
            terminate_one(child)
        if not live_children and not rediscovered:
            quiet_descendant_scans += 1
            if quiet_descendant_scans >= 2:
                terminate_one(pid)
                break
        else:
            quiet_descendant_scans = 0
        pause(0.02)
    targets = {pid, *known}
    quiet_tree_scans = 0
    while time.monotonic() < deadline:
        remaining = {item for item in targets if is_alive(item)}
        if not remaining:
            quiet_tree_scans += 1
            if quiet_tree_scans >= 2:
                return
        else:
            quiet_tree_scans = 0
            for item in sorted(remaining - {pid}):
                terminate_one(item)
            if pid in remaining:
                terminate_one(pid)
        pause(0.02)
    remaining = {item for item in targets if is_alive(item)}
    if remaining:
        raise RuntimeError(
            f"process tree did not terminate; remaining PIDs: {sorted(remaining)}"
        )


def probe_process_identity(pid: int) -> Mapping[str, object] | None:
    """Read the durable identity embedded in an executor wrapper command line."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process(pid)
        command = process.cmdline()
        values: dict[str, object] = {
            "pid": pid,
            "process_start_time": f"{process.create_time():.6f}",
        }
        for flag, key in (
            ("--command-fingerprint", "command_fingerprint"),
            ("--task-token", "task_token"),
        ):
            try:
                values[key] = command[command.index(flag) + 1]
            except (ValueError, IndexError):
                values[key] = None
        return values
    except (OSError, psutil.Error):
        return None


def _terminate_windows_process(
    pid: int, *, windows_api: _Win32ProcessApi | None = None
) -> None:
    api = windows_api or _CtypesWin32ProcessApi()
    handle = api.open_process(0x0001, pid)
    if not handle:
        error = api.get_last_error()
        if error in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_FOUND}:
            return
        _raise_windows_process_error("opening for termination", pid, error)
    try:
        if not api.terminate_process(handle, 1):
            error = api.get_last_error()
            if api.get_exit_code(handle) != _STILL_ACTIVE:
                return
            _raise_windows_process_error("terminating", pid, error)
    finally:
        api.close_handle(handle)


def _descendant_pids(
    pid: int, *, windows_api: _Win32ProcessApi | None = None
) -> set[int]:
    if os.name == "nt" or windows_api is not None:
        api = windows_api or _CtypesWin32ProcessApi()
        parents = {
            process_id: parent_id for process_id, parent_id in api.snapshot_processes()
        }
        descendants: set[int] = set()
        pending = [pid]
        while pending:
            parent = pending.pop()
            children = [child for child, owner in parents.items() if owner == parent]
            new_children = [child for child in children if child not in descendants]
            descendants.update(new_children)
            pending.extend(new_children)
        return descendants
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return {child.pid for child in psutil.Process(pid).children(recursive=True)}
        except (OSError, psutil.Error):
            pass
    parents: dict[int, int] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text(encoding="ascii").split()
            parents[int(fields[0])] = int(fields[3])
        except (OSError, ValueError, IndexError):
            continue
    descendants: set[int] = set()
    pending = [pid]
    while pending:
        parent = pending.pop()
        children = [child for child, owner in parents.items() if owner == parent]
        descendants.update(children)
        pending.extend(children)
    return descendants


def _pid_alive(pid: int, *, windows_api: _Win32ProcessApi | None = None) -> bool:
    if os.name == "nt" or windows_api is not None:
        api = windows_api or _CtypesWin32ProcessApi()
        handle = api.open_process(0x1000, pid)
        if not handle:
            error = api.get_last_error()
            if error in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_FOUND}:
                return False
            _raise_windows_process_error("inspecting liveness", pid, error)
        try:
            return api.get_exit_code(handle) == _STILL_ACTIVE
        finally:
            api.close_handle(handle)
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except (OSError, psutil.Error):
            # AccessDenied and other inspection failures are not proof that the
            # PID is absent. Recovery must retain capacity until absence is known.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


_ERROR_ACCESS_DENIED = 5
_ERROR_NO_MORE_FILES = 18
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_FOUND = 1168
_STILL_ACTIVE = 259


def _raise_windows_process_error(action: str, pid: int, error: int) -> None:
    message = (
        f"{action} access denied for PID {pid}"
        if error == _ERROR_ACCESS_DENIED
        else (f"{action} failed for PID {pid} with Win32 error {error}")
    )
    if error == _ERROR_ACCESS_DENIED:
        raise PermissionError(error, message)
    raise OSError(error, message)


class _CtypesWin32ProcessApi:
    """Small native Win32 process API with no psutil dependency."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._ctypes = ctypes
        self._kernel32 = kernel32
        self._entry_type = PROCESSENTRY32W

    def snapshot_processes(self) -> tuple[tuple[int, int], ...]:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot in (None, self._ctypes.c_void_p(-1).value):
            error = self.get_last_error()
            raise OSError(error, f"process snapshot failed with Win32 error {error}")
        processes: list[tuple[int, int]] = []
        try:
            entry = self._entry_type()
            entry.dwSize = self._ctypes.sizeof(self._entry_type)
            if not self._kernel32.Process32FirstW(snapshot, self._ctypes.byref(entry)):
                error = self.get_last_error()
                if error == _ERROR_NO_MORE_FILES:
                    return ()
                raise OSError(
                    error,
                    f"process snapshot enumeration failed with Win32 error {error}",
                )
            while True:
                processes.append(
                    (int(entry.th32ProcessID), int(entry.th32ParentProcessID))
                )
                if self._kernel32.Process32NextW(snapshot, self._ctypes.byref(entry)):
                    continue
                error = self.get_last_error()
                if error != _ERROR_NO_MORE_FILES:
                    raise OSError(
                        error,
                        f"process snapshot enumeration failed with Win32 error {error}",
                    )
                break
        finally:
            self.close_handle(snapshot)
        return tuple(processes)

    def open_process(self, desired_access: int, pid: int) -> object | None:
        return self._kernel32.OpenProcess(desired_access, False, pid)

    def get_exit_code(self, handle: object) -> int:
        value = self._ctypes.c_ulong()
        if not self._kernel32.GetExitCodeProcess(handle, self._ctypes.byref(value)):
            error = self.get_last_error()
            raise OSError(error, f"GetExitCodeProcess failed with Win32 error {error}")
        return int(value.value)

    def terminate_process(self, handle: object, exit_code: int) -> bool:
        return bool(self._kernel32.TerminateProcess(handle, exit_code))

    def close_handle(self, handle: object) -> None:
        if not self._kernel32.CloseHandle(handle):
            error = self.get_last_error()
            raise OSError(error, f"CloseHandle failed with Win32 error {error}")

    def get_last_error(self) -> int:
        return int(self._ctypes.get_last_error())


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _probe_or_unverified(
    probe: Callable[[int], Mapping[str, object] | None], pid: int
) -> Mapping[str, object] | None:
    try:
        return probe(pid)
    except Exception:
        # AccessDenied and transient inspection failures cannot prove either
        # identity or process absence.
        return None


def _process_may_be_alive(alive: Callable[[int], bool], pid: int) -> bool:
    try:
        return alive(pid)
    except Exception:
        # Fail closed: an unreadable PID retains resource/exclusive ownership.
        return True


def _invalidated_job(job: QueueJob) -> QueueJob:
    status = "stale_input" if job.status == "success" else "superseded"
    return replace(
        job,
        status=status,
        stage=status,
        output_revision=None,
        output_fingerprint=None,
        output_validated=False,
        validated_input_fingerprint=None,
        published_outputs={},
        validation_proof=None,
        error=None,
        cleanup_reason=None,
        target_terminal_status=None,
    )


def _cleanup_terminal_job(job: QueueJob, status: str) -> QueueJob:
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"cleanup target is not terminal: {status}")
    error = (
        "cancellation interrupted by service restart"
        if status == "interrupted"
        else None
    )
    return replace(
        job.with_status(status),
        output_revision=None,
        output_fingerprint=None,
        output_validated=False,
        validated_input_fingerprint=None,
        published_outputs={},
        validation_proof=None,
        error=error,
        cleanup_reason=None,
        target_terminal_status=None,
    )
