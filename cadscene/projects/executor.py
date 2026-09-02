from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Protocol
from uuid import uuid4

from .adapters import AdapterProgress, AdapterResult
from .queue import ACTIVE_STATUSES, QueueJob, terminate_process_tree


@dataclass(frozen=True)
class JobExecutionPlan:
    commands: tuple[tuple[str, ...], ...]
    validate: Callable[[], AdapterResult]

    def __post_init__(self) -> None:
        if not self.commands or any(not command for command in self.commands):
            raise ValueError("execution plan requires non-empty commands")


class ExecutionCoordinator(Protocol):
    queue: object

    def reap_adopted_jobs(self) -> tuple[str, ...]:
        ...

    def prepare_job_execution(
        self,
        project_id: str,
        job_id: str,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> JobExecutionPlan:
        ...

    def record_job_process(
        self,
        project_id: str,
        job_id: str,
        *,
        pid: int,
        process_start_time: str,
        command_fingerprint: str,
        task_token: str,
        log_path: str,
        attempt_number: int,
        claim_token: str,
        terminate: Callable[[], None] | None = None,
    ) -> QueueJob:
        ...

    def update_job_progress(
        self,
        project_id: str,
        job_id: str,
        progress: AdapterProgress,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        ...

    def finish_job(
        self,
        project_id: str,
        job_id: str,
        result: AdapterResult,
        *,
        current_fingerprint: str | None = None,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        ...

    def fail_job(
        self,
        project_id: str,
        job_id: str,
        error: str,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        ...

    def release_job_process_controller(
        self,
        project_id: str,
        job_id: str,
        pid: int,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> None:
        ...

    def reclaim_job_attempt(
        self,
        project_id: str,
        job_id: str,
        *,
        attempt_number: int,
    ) -> object:
        ...


class LocalJobExecutor:
    """Runs one queue-reserved job; all durable state goes through the service."""

    def __init__(self, coordinator: ExecutionCoordinator) -> None:
        self.coordinator = coordinator

    def run_next(self) -> QueueJob | None:
        queue = self.coordinator.queue
        self.coordinator.reap_adopted_jobs()
        job = queue.claim_next_unstarted()
        if job is None:
            return None
        attempt_number = job.attempts[-1].number
        claim_token = job.attempts[-1].worker_claim_token
        if claim_token is None:  # queue claim invariant
            raise RuntimeError("claimed attempt is missing its worker token")
        process: subprocess.Popen | None = None
        controller: _WindowsJobObject | None = None
        log_handle = None
        try:
            self.coordinator.update_job_progress(
                job.project_id,
                job.job_id,
                AdapterProgress(stage="preparing", message="preparing adapter inputs"),
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            plan = self.coordinator.prepare_job_execution(
                job.project_id,
                job.job_id,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            attempt_dir = Path(job.attempts[-1].directory)
            attempt_dir.mkdir(parents=True, exist_ok=True)
            commands = [[str(item) for item in command] for command in plan.commands]
            serialized = json.dumps(
                commands, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            command_fingerprint = sha256(serialized).hexdigest()
            task_token = uuid4().hex
            plan_path = attempt_dir / "commands.json"
            plan_path.write_text(
                json.dumps({"commands": commands}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log_path = attempt_dir / "adapter.log"
            log_handle = log_path.open("ab", buffering=0)
            wrapper = [
                sys.executable,
                "-m",
                "cadscene.projects.process_worker",
                "--plan",
                str(plan_path),
                "--task-token",
                task_token,
                "--command-fingerprint",
                command_fingerprint,
            ]
            options: dict[str, object] = {}
            if os.name == "nt":
                options["creationflags"] = int(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ) | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                options["start_new_session"] = True
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "CADSCENE_TASK_TOKEN": task_token,
                }
            )
            process = subprocess.Popen(
                wrapper,
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                **options,
            )
            controller = (
                _WindowsJobObject.try_assign(process) if os.name == "nt" else None
            )
            self.coordinator.record_job_process(
                job.project_id,
                job.job_id,
                pid=process.pid,
                process_start_time=_process_start_time(process.pid),
                command_fingerprint=command_fingerprint,
                task_token=task_token,
                log_path=str(log_path),
                attempt_number=attempt_number,
                claim_token=claim_token,
                terminate=None if controller is None else controller.terminate,
            )
            self.coordinator.update_job_progress(
                job.project_id,
                job.job_id,
                AdapterProgress(stage="running", message="adapter process is running"),
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            returncode = self._wait_for_process(
                process,
                attempt_dir / "adapter_progress.json",
                project_id=job.project_id,
                job_id=job.job_id,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            current = queue.get(job.job_id)
            if current.status in {
                "cancelled",
                "interrupted",
                "superseded",
                "stale_input",
            }:
                return current
            if returncode != 0:
                return self.coordinator.fail_job(
                    job.project_id,
                    job.job_id,
                    f"adapter command failed with return code {returncode}",
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
            self.coordinator.update_job_progress(
                job.project_id,
                job.job_id,
                AdapterProgress(
                    stage="validating",
                    message="validating and publishing adapter output",
                    fraction=0.99,
                ),
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            result = plan.validate()
            return self.coordinator.finish_job(
                job.project_id,
                job.job_id,
                result,
                current_fingerprint=job.input_fingerprint,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
        except Exception as exc:
            cleanup_error: Exception | None = None
            if process is not None and process.poll() is None:
                try:
                    if controller is not None:
                        controller.terminate()
                    else:
                        terminate_process_tree(process.pid)
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            current = queue.get(job.job_id)
            if current.status in ACTIVE_STATUSES:
                message = str(exc)
                if cleanup_error is not None:
                    message = f"{message}; process cleanup failed: {cleanup_error}"
                return self.coordinator.fail_job(
                    job.project_id,
                    job.job_id,
                    message,
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
            return current
        finally:
            if controller is not None:
                controller.close()
            if process is not None:
                self.coordinator.release_job_process_controller(
                    job.project_id,
                    job.job_id,
                    process.pid,
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
            if log_handle is not None:
                log_handle.close()
            try:
                queue.release_execution_claim(
                    job.job_id,
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
            except ValueError:
                pass
            try:
                self.coordinator.reclaim_job_attempt(
                    job.project_id,
                    job.job_id,
                    attempt_number=attempt_number,
                )
            except Exception:
                # 回收属于 best-effort 维护，不能覆盖已经持久化的任务结果。
                pass

    def _wait_for_process(
        self,
        process: subprocess.Popen,
        progress_path: Path,
        *,
        project_id: str,
        job_id: str,
        attempt_number: int,
        claim_token: str,
    ) -> int:
        last_payload: str | None = None
        while True:
            returncode = process.poll()
            if progress_path.is_file():
                try:
                    serialized = progress_path.read_text(encoding="utf-8")
                    if serialized != last_payload:
                        candidate = json.loads(serialized)
                        progress = AdapterProgress(
                            stage=str(candidate["stage"]),
                            message=str(candidate["message"]),
                            fraction=(
                                None
                                if candidate.get("fraction") is None
                                else float(candidate["fraction"])
                            ),
                        )
                        self.coordinator.update_job_progress(
                            project_id,
                            job_id,
                            progress,
                            attempt_number=attempt_number,
                            claim_token=claim_token,
                        )
                        last_payload = serialized
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    # The adapter owns this transient sidecar. Ignore an invalid or
                    # concurrently replaced read and retry on the next poll.
                    pass
            if returncode is not None:
                return int(returncode)
            time.sleep(0.25)


def _process_start_time(pid: int) -> str:
    try:
        import psutil

        return f"{psutil.Process(pid).create_time():.6f}"
    except (ImportError, OSError):
        if os.name != "nt":
            stat = Path(f"/proc/{pid}/stat")
            if stat.is_file():
                return stat.read_text(encoding="ascii").split()[21]
        raise RuntimeError("unable to record child process start time")


class _WindowsJobObject:
    def __init__(self, handle: int, process: subprocess.Popen) -> None:
        self._handle = handle
        self._process = process

    @classmethod
    def try_assign(cls, process: subprocess.Popen) -> _WindowsJobObject | None:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        information = EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        configured = kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle, int(process._handle)
        )
        if not assigned:
            kernel32.CloseHandle(handle)
            return None
        return cls(handle, process)

    def terminate(self) -> None:
        if not self._handle:
            return
        import ctypes

        if not ctypes.windll.kernel32.TerminateJobObject(self._handle, 1):
            raise RuntimeError("failed to terminate Windows job object")
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Windows job object did not terminate") from exc
        finally:
            self.close()

    def close(self) -> None:
        if self._handle:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = 0
