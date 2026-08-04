from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Protocol

from .identifiers import validate_project_id
from .json_repositories import ProjectRepositories
from .recovery import reconcile_project
from .service import ProjectService


_LEASES_LOCK = threading.Lock()
_PROCESS_LEASES: set[str] = set()


class QueueExecutor(Protocol):
    def run_next(self) -> object | None: ...


class ClosableAnalysis(Protocol):
    def close(self, *, wait: bool = True) -> None: ...


class ProjectRootLease:
    """Cross-process exclusive ownership of a single-machine project root."""

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = Path(projects_root).resolve()
        self.path = self.projects_root / ".serve_viewer.lease"
        self._stream = None
        self._key = os.path.normcase(str(self.path))

    def acquire(self) -> None:
        if self._stream is not None:
            return
        self.projects_root.mkdir(parents=True, exist_ok=True)
        with _LEASES_LOCK:
            if self._key in _PROCESS_LEASES:
                raise RuntimeError("project root is already owned by another serve_viewer")
            stream = self.path.open("a+b")
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                _lock_stream(stream)
                stream.seek(0)
                stream.truncate()
                stream.write(
                    json.dumps({"pid": os.getpid()}, separators=(",", ":")).encode(
                        "ascii"
                    )
                )
                stream.flush()
            except BaseException:
                stream.close()
                raise RuntimeError(
                    "project root is already owned by another serve_viewer"
                )
            _PROCESS_LEASES.add(self._key)
            self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            _unlock_stream(stream)
        finally:
            stream.close()
            self._stream = None
            with _LEASES_LOCK:
                _PROCESS_LEASES.discard(self._key)

    def __enter__(self) -> ProjectRootLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _lock_stream(stream) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ProjectRuntime:
    """Owns recovery and the single-machine queue worker lifecycle."""

    def __init__(
        self,
        *,
        projects_root: Path,
        repositories: ProjectRepositories,
        service: ProjectService,
        executor: QueueExecutor,
        analysis: ClosableAnalysis | None,
        lease: ProjectRootLease | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("worker poll interval must be positive")
        self.projects_root = Path(projects_root)
        self.repositories = repositories
        self.service = service
        self.executor = executor
        self.analysis = analysis
        self.lease = lease or ProjectRootLease(self.projects_root)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_worker_error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.lease.acquire()
        try:
            for project_id in self._project_ids():
                reconcile_project(project_id, repositories=self.repositories)
                self.service.restore_jobs(
                    project_id,
                    process_probe=lambda _pid: None,
                    unverified_process_policy="interrupt",
                )
            self._thread = threading.Thread(
                target=self._run_worker,
                name="project-job-worker",
                daemon=False,
            )
            self._thread.start()
        except BaseException:
            self.lease.release()
            raise

    def close(self, *, timeout: float = 30.0) -> None:
        thread = self._thread
        if thread is None:
            if self.analysis is not None:
                self.analysis.close(wait=True)
                self.analysis = None
            self.lease.release()
            return
        self._stop.set()
        grace = min(0.25, max(0.0, timeout / 4.0))
        thread.join(grace)
        cancellation_errors: list[Exception] = []
        if thread.is_alive():
            for job_id in self.service.queue.execution_claimed_ids():
                try:
                    job = self.service.queue.get(job_id)
                    self.service.cancel_job(job.project_id, job_id)
                except Exception as exc:
                    cancellation_errors.append(exc)
            thread.join(max(0.0, timeout - grace))
        if thread.is_alive():
            details = (
                "; ".join(str(error) for error in cancellation_errors)
                or "claimed work did not terminate"
            )
            raise RuntimeError(f"project job worker did not stop cleanly: {details}")
        self._thread = None
        try:
            if self.analysis is not None:
                self.analysis.close(wait=True)
                self.analysis = None
        finally:
            self.lease.release()
        if cancellation_errors:
            raise RuntimeError(
                "one or more claimed jobs failed cancellation: "
                + "; ".join(str(error) for error in cancellation_errors)
            )

    def _project_ids(self) -> tuple[str, ...]:
        if not self.projects_root.is_dir():
            return ()
        project_ids: list[str] = []
        for path in self.projects_root.iterdir():
            if not path.is_dir() or not (path / "project_manifest.json").is_file():
                continue
            try:
                project_ids.append(validate_project_id(path.name))
            except ValueError:
                continue
        return tuple(sorted(project_ids))

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.executor.run_next()
                self.last_worker_error = None
            except Exception as exc:  # keep the service available; state is durable
                self.last_worker_error = exc
                result = None
            if result is None:
                self._stop.wait(self.poll_interval)
