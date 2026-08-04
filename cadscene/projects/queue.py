from __future__ import annotations

from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import signal
import threading
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .adapters import AdapterProgress


RESOURCE_CLASSES = ("heavy_compute", "light_compute", "media_io", "control")
ACTIVE_STATUSES = {"preparing", "running", "validating"}
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
        )


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
    output_fingerprint: str | None = None
    output_validated: bool = False
    validated_input_fingerprint: str | None = None
    published_outputs: Mapping[str, str] = field(default_factory=dict)
    progress: Mapping[str, object] | None = None
    error: str | None = None

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
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "published_outputs": dict(self.published_outputs),
            "progress": None if self.progress is None else dict(self.progress),
            "error": self.error,
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
            progress=(
                None
                if value.get("progress") is None
                else dict(value["progress"])  # type: ignore[arg-type]
            ),
            error=_optional_string(value.get("error")),
        )


class TaskQueue(Protocol):
    def submit(self, job: QueueJob) -> QueueJob: ...

    def status(self, job_id: str) -> str: ...

    def running_ids(self) -> list[str]: ...

    def cancel(self, job_id: str) -> QueueJob: ...


class LocalResourceQueue:
    def __init__(
        self,
        *,
        capacities: Mapping[str, int] | None = None,
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        process_tree_terminator: Callable[[int], None] | None = None,
    ) -> None:
        configured = {name: 1 for name in RESOURCE_CLASSES}
        configured.update(capacities or {})
        if set(configured) != set(RESOURCE_CLASSES):
            raise ValueError("capacities must use only static resource classes")
        if any(isinstance(value, bool) or int(value) < 1 for value in configured.values()):
            raise ValueError("resource capacities must be positive integers")
        self.capacities = {name: int(value) for name, value in configured.items()}
        self._jobs: dict[str, QueueJob] = {}
        self._queue_order: list[str] = []
        self._terminate_tree = process_tree_terminator or terminate_process_tree
        self._process_probe = process_probe or probe_process_identity
        self._process_controllers: dict[int, Callable[[], None]] = {}
        self._execution_claims: set[str] = set()
        self._lock = threading.RLock()

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
        schedule: bool = True,
    ) -> LocalResourceQueue:
        queue = cls(
            capacities=capacities,
            process_probe=process_probe,
            process_tree_terminator=process_tree_terminator,
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
        if current_fingerprint_resolver is not None:
            for job_id in queue._queue_order:
                current = queue._jobs[job_id]
                fingerprint = current_fingerprint_resolver(current)
                if fingerprint != current.input_fingerprint:
                    queue._jobs[job_id] = _invalidated_job(current)
            changed = True
            while changed:
                changed = False
                for job_id in queue._queue_order:
                    current = queue._jobs[job_id]
                    if current.status in {"stale_input", "superseded"}:
                        continue
                    if any(
                        queue._jobs[dependency].status
                        in {"stale_input", "superseded"}
                        for dependency in current.depends_on_job_ids
                    ):
                        queue._jobs[job_id] = _invalidated_job(current)
                        changed = True
        probe = process_probe or (lambda _pid: None)
        for job_id in queue._queue_order:
            current = queue._jobs[job_id]
            if current.status not in {"preparing", "running", "validating"}:
                continue
            attempt = current.attempts[-1] if current.attempts else None
            if attempt is None or attempt.pid is None:
                queue._jobs[job_id] = current.with_status("interrupted")
                continue
            observed = probe(attempt.pid)
            expected = {
                "pid": attempt.pid,
                "process_start_time": attempt.process_start_time,
                "command_fingerprint": attempt.command_fingerprint,
                "task_token": attempt.task_token,
            }
            if (
                any(value in (None, "") for value in expected.values())
                or observed is None
                or any(observed.get(key) != value for key, value in expected.items())
            ):
                queue._jobs[job_id] = current.with_status("interrupted")
        if schedule:
            queue._schedule()
        return queue

    def merge_restored(
        self,
        jobs: Iterable[QueueJob | Mapping[str, object]],
        *,
        queue_order: Sequence[str],
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        current_fingerprint_resolver: Callable[[QueueJob], str | None] | None = None,
    ) -> LocalResourceQueue:
        incoming_values = [
            item if isinstance(item, QueueJob) else QueueJob.from_dict(item)
            for item in jobs
        ]
        incoming_projects = {item.project_id for item in incoming_values}
        restored = LocalResourceQueue.restore(
            incoming_values,
            queue_order=queue_order,
            capacities=self.capacities,
            process_probe=process_probe,
            current_fingerprint_resolver=current_fingerprint_resolver,
            process_tree_terminator=self._terminate_tree,
            schedule=False,
        )
        retained_order = [
            job_id
            for job_id in self._queue_order
            if self._jobs[job_id].project_id not in incoming_projects
        ]
        retained_jobs = {job_id: self._jobs[job_id] for job_id in retained_order}
        retained_controllers = {
            pid: controller
            for pid, controller in self._process_controllers.items()
            if any(
                attempt.pid == pid
                for job in retained_jobs.values()
                for attempt in job.attempts
            )
        }
        self._jobs = {**retained_jobs, **restored._jobs}
        self._queue_order = [*retained_order, *restored._queue_order]
        self._process_controllers = retained_controllers
        if process_probe is not None:
            self._process_probe = process_probe
        self._schedule()
        return self

    def submit(self, job: QueueJob) -> QueueJob:
        for existing in self._jobs.values():
            if existing.idempotency_key == job.idempotency_key:
                if (
                    existing.input_fingerprint != job.input_fingerprint
                    or existing.adapter_name != job.adapter_name
                    or existing.adapter_version != job.adapter_version
                ):
                    raise ValueError("idempotency key collision across input or adapter identity")
                return existing
        if job.job_id in self._jobs:
            raise ValueError(f"duplicate job_id: {job.job_id}")
        missing = set(job.depends_on_job_ids) - set(self._jobs)
        if missing:
            raise ValueError(f"unknown dependencies: {sorted(missing)}")
        self._jobs[job.job_id] = job
        self._queue_order.append(job.job_id)
        self._schedule()
        return self._jobs[job.job_id]

    def status(self, job_id: str) -> str:
        return self._jobs[job_id].status

    def get(self, job_id: str) -> QueueJob:
        return self._jobs[job_id]

    def jobs(self) -> tuple[QueueJob, ...]:
        return tuple(self._jobs[job_id] for job_id in self._queue_order)

    def queue_order(self) -> tuple[str, ...]:
        return tuple(self._queue_order)

    def running_ids(self) -> list[str]:
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
                    and (attempt is None or attempt.pid is None)
                    and job_id not in self._execution_claims
                ):
                    self._execution_claims.add(job_id)
                    return current
        return None

    def release_execution_claim(self, job_id: str) -> None:
        with self._lock:
            self._execution_claims.discard(job_id)

    def mark_validating(self, job_id: str) -> QueueJob:
        current = self._jobs[job_id]
        if current.status != "running":
            raise ValueError("only running jobs can begin validation")
        self._jobs[job_id] = current.with_status("validating")
        return self._jobs[job_id]

    def update_progress(
        self, job_id: str, progress: AdapterProgress
    ) -> QueueJob:
        current = self._jobs[job_id]
        if current.status not in ACTIVE_STATUSES:
            raise ValueError("progress can be updated only for an active job")
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
        pid: int,
        process_start_time: str,
        command_fingerprint: str,
        task_token: str,
        log_path: str,
        terminate: Callable[[], None] | None = None,
    ) -> QueueJob:
        with self._lock:
            current = self._jobs[job_id]
            if current.status not in ACTIVE_STATUSES or not current.attempts:
                raise ValueError("process identity requires an active attempt")
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
                self._process_controllers[pid] = terminate
            return self._jobs[job_id]

    def register_process_controller(
        self, job_id: str, terminate: Callable[[], None]
    ) -> None:
        with self._lock:
            current = self._jobs[job_id]
            attempt = current.attempts[-1] if current.attempts else None
            if attempt is None or attempt.pid is None:
                raise ValueError("process controller requires recorded process identity")
            self._process_controllers[attempt.pid] = terminate

    def release_process_controller(self, pid: int) -> None:
        """Forget a completed attempt's in-memory cancellation callback."""
        with self._lock:
            self._process_controllers.pop(pid, None)

    def mark_success(
        self,
        job_id: str,
        *,
        output_revision: str,
        output_fingerprint: str | None = None,
        output_validated: bool = True,
        published_outputs: Mapping[str, str] | None = None,
    ) -> QueueJob:
        current = self._jobs[job_id]
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
            error=None,
        )
        self._schedule()
        return self._jobs[job_id]

    def validate_output(
        self, job_id: str, *, current_input_fingerprint: str
    ) -> QueueJob:
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
        self._schedule()
        return self._jobs[job_id]

    def mark_failed(self, job_id: str, error: str) -> QueueJob:
        current = self._jobs[job_id]
        if current.status not in ACTIVE_STATUSES:
            raise ValueError("only active jobs can fail")
        self._jobs[job_id] = replace(
            current.with_status("failed"), error=error, output_validated=False
        )
        self._schedule()
        return self._jobs[job_id]

    def mark_stale_input(self, job_id: str) -> QueueJob:
        current = self._jobs[job_id]
        if current.status not in ACTIVE_STATUSES:
            raise ValueError("only active jobs can become stale")
        self._jobs[job_id] = replace(
            current.with_status("stale_input"),
            output_revision=None,
            output_fingerprint=None,
            output_validated=False,
            validated_input_fingerprint=None,
            published_outputs={},
            error=None,
        )
        self._schedule()
        return self._jobs[job_id]

    def mark_superseded(self, job_id: str) -> QueueJob:
        current = self._jobs[job_id]
        if current.status in TERMINAL_STATUSES:
            return current
        self._jobs[job_id] = replace(
            current.with_status("superseded"),
            output_revision=None,
            output_fingerprint=None,
            output_validated=False,
            validated_input_fingerprint=None,
            published_outputs={},
            error=None,
        )
        self._schedule()
        return self._jobs[job_id]

    def cancel(self, job_id: str) -> QueueJob:
        current = self._jobs[job_id]
        if current.status in TERMINAL_STATUSES:
            return current
        attempt = current.attempts[-1] if current.attempts else None
        if attempt is not None and attempt.pid is not None:
            try:
                controller = self._process_controllers.pop(attempt.pid, None)
                if controller is not None:
                    controller()
                else:
                    observed = self._process_probe(attempt.pid)
                    expected = {
                        "pid": attempt.pid,
                        "process_start_time": attempt.process_start_time,
                        "command_fingerprint": attempt.command_fingerprint,
                        "task_token": attempt.task_token,
                    }
                    if (
                        any(value in (None, "") for value in expected.values())
                        or observed is None
                        or any(
                            observed.get(key) != value
                            for key, value in expected.items()
                        )
                    ):
                        raise RuntimeError(
                            "refusing PID fallback because process identity could not be verified"
                        )
                    self._terminate_tree(attempt.pid)
            except Exception as exc:
                self._jobs[job_id] = replace(
                    current.with_status("interrupted"),
                    error=str(exc),
                    output_revision=None,
                    output_fingerprint=None,
                    output_validated=False,
                    published_outputs={},
                )
                self._schedule()
                return self._jobs[job_id]
        self._jobs[job_id] = replace(
            current.with_status("cancelled"),
            output_revision=None,
            output_fingerprint=None,
            output_validated=False,
            published_outputs={},
        )
        self._schedule()
        return self._jobs[job_id]

    def retry(self, job_id: str, attempt: AttemptRecord) -> QueueJob:
        current = self._jobs[job_id]
        if current.status not in {
            "failed",
            "interrupted",
            "cancelled",
            "stale_input",
            "superseded",
        }:
            raise ValueError("only terminal unsuccessful jobs can be retried")
        retried = replace(
            current.with_attempt(attempt),
            status="queued",
            stage="queued",
            output_revision=None,
            output_fingerprint=None,
            output_validated=False,
            validated_input_fingerprint=None,
            published_outputs={},
            error=None,
        )
        self._jobs[job_id] = retried
        self._schedule()
        return self._jobs[job_id]

    def _schedule(self) -> None:
        usage = {name: 0 for name in RESOURCE_CLASSES}
        exclusive = set()
        for current in self._jobs.values():
            if current.status in ACTIVE_STATUSES:
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
            usage[current.resource_class] += 1
            if current.exclusive_key is not None:
                exclusive.add(current.exclusive_key)

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
    descendants = _descendant_pids(pid)
    targets = {pid, *descendants}
    if os.name == "nt":
        for target in (*descendants, pid):
            _terminate_windows_process(target)
    else:
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


def probe_process_identity(pid: int) -> Mapping[str, object] | None:
    """Read the durable identity embedded in an executor wrapper command line."""
    try:
        import psutil

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
    except (ImportError, OSError):
        return None


def _terminate_windows_process(pid: int) -> None:
    try:
        import psutil

        psutil.Process(pid).terminate()
        return
    except ImportError:
        pass
    except OSError:
        return
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        if _pid_alive(pid):
            raise RuntimeError(f"access denied terminating PID {pid}")
        return
    try:
        if not ctypes.windll.kernel32.TerminateProcess(handle, 1):
            raise RuntimeError(f"failed terminating PID {pid}")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _descendant_pids(pid: int) -> set[int]:
    try:
        import psutil

        return {child.pid for child in psutil.Process(pid).children(recursive=True)}
    except (ImportError, OSError):
        if os.name == "nt":
            return set()
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


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except OSError:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


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
        error=None,
    )
