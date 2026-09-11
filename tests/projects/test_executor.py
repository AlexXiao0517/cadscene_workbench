from __future__ import annotations

import json
import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
import sys
import threading
import time

import pytest

import cadscene.projects.executor as executor_module
import cadscene.projects.process_worker as process_worker
from cadscene.projects.adapters import (
    AdapterInputs,
    AdapterResult,
    WorkflowAdapterRegistry,
)
from cadscene.projects.executor import JobExecutionPlan, LocalJobExecutor
from tests.projects.test_service_jobs import clip, service_with_clips


class _ExecutableAdapter:
    name = "sfm_only"
    version = "test-1"
    requires_physical_mp4 = True
    srt_requirement = "none"
    available = True
    unavailable_reason = None

    def prepare_inputs(self, inputs: AdapterInputs) -> AdapterInputs:
        assert inputs.video_path.is_file()
        assert inputs.source_start_pts == 50
        assert inputs.source_end_pts_exclusive == 100
        assert inputs.source_time_base == Fraction(1, 25)
        assert inputs.frame_map_path is not None and inputs.frame_map_path.is_file()
        inputs.attempt_directory.mkdir(parents=True, exist_ok=True)
        return inputs

    def build_commands(self, inputs: AdapterInputs):
        output = inputs.attempt_directory / "trajectory.json"
        return (
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(output)!r}).write_text('{{\"poses\":[{{}}]}}', encoding='utf-8')",
            ),
        )

    def build_command(self, inputs: AdapterInputs):
        return self.build_commands(inputs)[0]

    def validate_outputs(self, inputs: AdapterInputs) -> AdapterResult:
        output = inputs.attempt_directory / "trajectory.json"
        if not output.is_file():
            return AdapterResult.failed("trajectory missing")
        return AdapterResult.success(
            output_revision="fake-output-1",
            output_fingerprint="fake-output-fingerprint",
            outputs={"trajectory": str(output)},
        )

    def describe_workbench(self):
        return {}

    def describe_render(self):
        return {}


def _wait_until(predicate, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def _pid_running(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return (
                bool(
                    ctypes.windll.kernel32.GetExitCodeProcess(
                        handle, ctypes.byref(code)
                    )
                )
                and code.value == 259
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_executor_runs_commands_sequentially_and_service_publishes_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    job_id = queue.running_ids()[0]
    attempt = Path(queue.get(job_id).attempts[-1].directory)
    events = attempt / "events.txt"
    output = attempt / "validated.json"
    first = (
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(events)!r}).write_text('first\\n', encoding='utf-8')",
    )
    second = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"p=Path({str(events)!r}); "
            "assert p.read_text(encoding='utf-8') == 'first\\n'; "
            "p.write_text('first\\nsecond\\n', encoding='utf-8'); "
            f"Path({str(output)!r}).write_text('{{\"ok\": true}}', encoding='utf-8')"
        ),
    )
    plan = JobExecutionPlan(
        commands=(first, second),
        validate=lambda: AdapterResult.success(
            output_revision="validated-1",
            output_fingerprint="validated-fingerprint",
            outputs={"artifact": str(output)},
        ),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )

    completed = LocalJobExecutor(service).run_next()

    assert completed is not None
    assert events.read_text(encoding="utf-8") == "first\nsecond\n"
    stored = next(
        item for item in repositories.jobs.load("p1").jobs if item["job_id"] == job_id
    )
    assert stored["status"] == "success"
    assert stored["published_outputs"] == {"artifact": str(output)}
    attempt_state = stored["attempts"][-1]
    assert attempt_state["pid"] > 0
    assert attempt_state["process_start_time"]
    assert attempt_state["command_fingerprint"]
    assert attempt_state["task_token"]
    assert Path(attempt_state["log_path"]).is_file()


def test_executor_forwards_structured_adapter_progress_while_process_runs(
    tmp_path: Path, monkeypatch
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    job_id = queue.running_ids()[0]
    attempt = Path(queue.get(job_id).attempts[-1].directory)
    progress_path = attempt / "adapter_progress.json"
    command = (
        sys.executable,
        "-c",
        (
            "import json,time; from pathlib import Path; "
            f"p=Path({str(progress_path)!r}); "
            "p.write_text(json.dumps({'schema_version':'1.0','stage':'parsing_video',"
            "'message':'正在分析抽样画面','fraction':0.42}),encoding='utf-8'); "
            "time.sleep(0.6)"
        ),
    )
    plan = JobExecutionPlan(
        commands=(command,),
        validate=lambda: AdapterResult.success(
            output_revision="validated-1",
            output_fingerprint="validated-fingerprint",
            outputs={},
        ),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )
    reported = []
    original = service.update_job_progress

    def record_progress(project_id, current_job_id, progress, **lease):
        reported.append(progress)
        return original(project_id, current_job_id, progress, **lease)

    monkeypatch.setattr(service, "update_job_progress", record_progress)

    completed = LocalJobExecutor(service).run_next()

    assert completed is not None and completed.status == "success"
    assert any(
        item.stage == "parsing_video" and item.fraction == 0.42
        for item in reported
    )


def test_executor_keeps_validation_progress_determinate_at_99_percent(
    tmp_path: Path, monkeypatch
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    plan = JobExecutionPlan(
        commands=((sys.executable, "-c", "pass"),),
        validate=lambda: AdapterResult.success(
            output_revision="validated-1",
            output_fingerprint="validated-fingerprint",
            outputs={},
        ),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )
    reported = []
    original = service.update_job_progress

    def record_progress(project_id, current_job_id, progress, **lease):
        reported.append(progress)
        return original(project_id, current_job_id, progress, **lease)

    monkeypatch.setattr(service, "update_job_progress", record_progress)

    completed = LocalJobExecutor(service).run_next()

    assert completed is not None and completed.status == "success"
    validating = next(item for item in reported if item.stage == "validating")
    assert validating.fraction == 0.99


def test_executor_claims_a_reserved_job_only_once_across_concurrent_workers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    job_id = queue.running_ids()[0]
    attempt = Path(queue.get(job_id).attempts[-1].directory)
    count_path = attempt / "count.txt"
    command = (
        sys.executable,
        "-c",
        (
            "from pathlib import Path; import time; "
            f"p=Path({str(count_path)!r}); "
            "p.write_text((p.read_text() if p.exists() else '') + 'run\\n'); "
            "time.sleep(0.2)"
        ),
    )
    plan = JobExecutionPlan(
        commands=(command,),
        validate=lambda: AdapterResult.success(
            output_revision="validated-1",
            output_fingerprint="validated-fingerprint",
        ),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )
    executor = LocalJobExecutor(service)
    threads = [threading.Thread(target=executor.run_next) for _ in range(2)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert count_path.read_text(encoding="utf-8") == "run\n"


def test_executor_terminates_launched_wrapper_when_identity_publication_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    plan = JobExecutionPlan(
        commands=((sys.executable, "-c", "pass"),),
        validate=lambda: AdapterResult.failed("must not validate"),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )

    class FakeProcess:
        pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(
        executor_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess()
    )
    monkeypatch.setattr(executor_module, "_process_start_time", lambda _pid: "start")
    monkeypatch.setattr(
        executor_module._WindowsJobObject,
        "try_assign",
        classmethod(lambda _cls, _process: None),
    )
    terminated: list[int] = []
    monkeypatch.setattr(executor_module, "terminate_process_tree", terminated.append)
    monkeypatch.setattr(
        service,
        "record_job_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("manifest publication failed")
        ),
    )

    completed = LocalJobExecutor(service).run_next()

    assert completed is not None and completed.status == "failed"
    assert terminated == [4242]
    assert completed.error == "manifest publication failed"


def test_executor_cancel_terminates_real_parent_and_descendant_tree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service, repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    job_id = queue.running_ids()[0]
    attempt = Path(queue.get(job_id).attempts[-1].directory)
    child_pid_path = attempt / "child.pid"
    command = (
        sys.executable,
        "-c",
        (
            "import pathlib, subprocess, sys, time; "
            "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8'); "
            "time.sleep(60)"
        ),
    )
    plan = JobExecutionPlan(
        commands=(command,),
        validate=lambda: AdapterResult.failed("cancelled plan must not validate"),
    )
    monkeypatch.setattr(
        service, "prepare_job_execution", lambda _project, _job, **_lease: plan
    )
    executor = LocalJobExecutor(service)
    thread = threading.Thread(target=executor.run_next, daemon=True)
    thread.start()
    _wait_until(lambda: child_pid_path.is_file())
    wrapper_pid = queue.get(job_id).attempts[-1].pid
    assert wrapper_pid is not None
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))

    cancelled = service.cancel_job("p1", job_id)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert cancelled.status == "cancelled"
    assert not _pid_running(wrapper_pid)
    assert not _pid_running(child_pid)
    stored = next(
        item for item in repositories.jobs.load("p1").jobs if item["job_id"] == job_id
    )
    assert stored["status"] == "cancelled"
    assert Path(stored["attempts"][-1]["log_path"]).exists()


def test_executor_uses_real_service_adapter_plan_without_manifest_bypass(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical.mp4"
    physical.write_bytes(b"clip")
    frame_map = tmp_path / "clip_frame_map.json"
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 25},
                "clips": [
                    {
                        "clip_id": "one",
                        "source_start_pts": 50,
                        "source_end_pts_exclusive": 100,
                        "frames": [{"ordinal": 50, "pts": 50}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    definition = clip("one")
    definition = replace(
        definition,
        analysis={
            **definition.analysis,
            "source_start_pts": 50,
            "physical_mp4_path": str(physical),
            "frame_map_path": str(frame_map),
        },
    )
    service, repositories, queue = service_with_clips(tmp_path, (definition,))
    service.adapters = WorkflowAdapterRegistry((_ExecutableAdapter(),))
    enqueued = service.enqueue_trajectory_jobs("p1")
    assert len(queue.jobs()) == 1

    completed = LocalJobExecutor(service).run_next()

    assert completed is not None and completed.status == "success"
    stored = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == enqueued.job_ids[0]
    )
    assert stored["published_outputs"]["trajectory"].endswith("trajectory.json")


def test_service_builds_existing_clip_export_cli_plan_inside_attempt(
    tmp_path: Path,
) -> None:
    service, _repositories, queue = service_with_clips(tmp_path, (clip("one"),))
    service.enqueue_trajectory_jobs("p1")
    export_id = queue.running_ids()[0]
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == export_id
    lease = claimed.attempts[-1]

    plan = service.prepare_job_execution(
        "p1",
        export_id,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    command = plan.commands[0]
    assert command[1:3] == ("-m", "cadscene.cli.export_video_clips")
    manifest_path = Path(command[command.index("--manifest") + 1])
    output_dir = Path(command[command.index("--output-dir") + 1])
    progress_path = Path(command[command.index("--progress-file") + 1])
    assert command[command.index("--preset") + 1] == "veryfast"
    assert manifest_path.parent == Path(queue.get(export_id).attempts[-1].directory)
    assert output_dir.parent == manifest_path.parent
    assert progress_path == manifest_path.parent / "adapter_progress.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["clips"][0]["interval_semantics"] == "half_open"


@pytest.mark.parametrize(
    "workflow", ("srt_full_pose", "srt_fixed_track_visual_pose")
)
def test_service_clip_export_reuses_unsegmented_srt_source_video(
    tmp_path: Path, workflow: str
) -> None:
    definition = clip("one", workflow=workflow)
    service, repositories, queue = service_with_clips(tmp_path, (definition,))
    export = service.enqueue_workbench_clip_export(
        "p1",
        "one",
        expected_jobs_revision=repositories.jobs.load("p1").revision,
    )
    assert export.adapter_version == "2"
    claimed = queue.claim_next_unstarted()
    assert claimed is not None
    lease = claimed.attempts[-1]

    plan = service.prepare_job_execution(
        "p1",
        claimed.job_id,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    command = plan.commands[0]
    assert "--no-duration-limit" in command
    assert "--reuse-source-full-span" in command


def test_process_worker_refuses_tampered_plan_before_starting_child(
    tmp_path: Path, monkeypatch
) -> None:
    plan = tmp_path / "commands.json"
    original = [[sys.executable, "-c", "print('safe')"]]
    canonical = json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    fingerprint = executor_module.sha256(canonical).hexdigest()
    plan.write_text(
        json.dumps({"commands": [[sys.executable, "-c", "print('tampered')"]]}),
        encoding="utf-8",
    )
    calls: list[object] = []

    def record_run(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(process_worker.subprocess, "run", record_run)

    with pytest.raises(ValueError, match="fingerprint"):
        process_worker.main(
            [
                "--plan",
                str(plan),
                "--task-token",
                "token",
                "--command-fingerprint",
                fingerprint,
            ]
        )

    assert calls == []
