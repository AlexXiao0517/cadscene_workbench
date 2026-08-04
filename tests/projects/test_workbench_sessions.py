from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.projects.workbench_sessions import (
    AtomicWorkbenchSessionStore,
    InvalidWorkbenchOutput,
    InvalidWorkbenchReturnPath,
    ReplayedWorkbenchSave,
    StaleWorkbenchSession,
    WorkbenchContext,
    WorkbenchPermissionDenied,
    ProjectWorkbenchService,
    WorkbenchSessionCoordinator,
)
from cadscene.projects.http_api import ProjectApi
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
from cadscene.projects.uploads import ValidatedUploadStore
from cadscene.projects.workflow_adapters import default_workflow_adapters


UTC = timezone.utc


def test_project_package_exports_workbench_coordination_interfaces() -> None:
    import cadscene.projects as projects

    assert projects.WorkbenchSessionStore is not None
    assert projects.AtomicWorkbenchSessionStore is AtomicWorkbenchSessionStore
    assert projects.ProjectWorkbenchService is ProjectWorkbenchService


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _context(**changes: object) -> WorkbenchContext:
    value = WorkbenchContext(
        project_id="project-1",
        clip_id="clip-1",
        workflow="sfm_only",
        project_input_revision="analysis-1",
        clip_input_revision="analysis-1",
        input_fingerprint="input-fingerprint-1",
        trajectory_job_id="job-1",
        trajectory_run_id="run-1",
        trajectory_output_revision="trajectory-1",
        trajectory_output_fingerprint="trajectory-fingerprint-1",
        save_permissions=("save",),
        can_open_workbench=True,
    )
    return replace(value, **changes)


@pytest.fixture
def session_system(tmp_path: Path):
    clock = MutableClock()
    current = [_context()]
    validated = []
    token_counter = [0]

    def resolve(project_id: str, clip_id: str) -> WorkbenchContext:
        assert project_id == current[0].project_id
        assert clip_id == current[0].clip_id
        return current[0]

    def validate(session, receipt):
        validated.append((session.token, dict(receipt)))
        if receipt.get("ok") is not True:
            raise InvalidWorkbenchOutput("workbench output was not validated")
        return {
            "source_output_revision": str(receipt["source_output_revision"]),
            "source_output_fingerprint": str(receipt["source_output_fingerprint"]),
            "artifacts": dict(receipt.get("artifacts", {})),
        }

    def token_factory() -> str:
        token_counter[0] += 1
        return f"unguessable-token-{token_counter[0]}-" + "x" * 32

    store = AtomicWorkbenchSessionStore(tmp_path / "projects", now=clock)
    coordinator = WorkbenchSessionCoordinator(
        store=store,
        outputs_root=tmp_path / "projects",
        resolve_context=resolve,
        validate_output=validate,
        now=clock,
        token_factory=token_factory,
        revision_factory=lambda: "workbench-output-1",
        ttl=timedelta(minutes=15),
    )
    return coordinator, store, clock, current, validated


def _create(coordinator: WorkbenchSessionCoordinator, **changes: object):
    return_to = str(
        changes.pop(
            "return_to",
            "/apps/project_workspace/?projectId=project-1&focusClip=clip-1",
        )
    )
    return coordinator.create(
        "project-1",
        "clip-1",
        return_to=return_to,
        **changes,
    )


def _receipt(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ok": True,
        "source_output_revision": "manual-track-7",
        "source_output_fingerprint": "f" * 64,
        "artifacts": {"camera_track": "01_keyframes/camera_track_manual.json"},
    }
    value.update(changes)
    return value


def test_session_binds_every_authoritative_input_and_uses_unguessable_token(
    session_system,
) -> None:
    coordinator, store, _clock, _current, _validated = session_system

    session = _create(coordinator)

    assert len(session.token) >= 32
    assert session.project_id == "project-1"
    assert session.clip_id == "clip-1"
    assert session.workflow == "sfm_only"
    assert session.project_input_revision == "analysis-1"
    assert session.clip_input_revision == "analysis-1"
    assert session.input_fingerprint == "input-fingerprint-1"
    assert session.trajectory_job_id == "job-1"
    assert session.trajectory_run_id == "run-1"
    assert session.trajectory_output_revision == "trajectory-1"
    assert session.trajectory_output_fingerprint == "trajectory-fingerprint-1"
    assert session.save_permissions == ("save",)
    assert session.state == "editing"
    assert store.load("project-1", session.token) == session
    assert session.token not in store.path_for("project-1", session.token).name


@pytest.mark.parametrize(
    "return_to",
    [
        "https://evil.test/apps/project_workspace/",
        "//evil.test/apps/project_workspace/",
        r"\apps\project_workspace\?projectId=project-1",
        "/apps/project_workspace/../admin",
        "/apps/project_workspace/%2e%2e/admin",
        "/apps/project_workspace/%252e%252e/admin",
        "/apps/web_camera_viewer/",
        "/api/projects/project-1/snapshot",
    ],
)
def test_return_to_rejects_external_traversal_encoded_and_unrelated_paths(
    session_system, return_to: str
) -> None:
    coordinator, *_ = session_system

    with pytest.raises(InvalidWorkbenchReturnPath):
        _create(coordinator, return_to=return_to)


def test_session_expires_fail_closed_and_unsaved_editing_recovers_to_ready(
    session_system,
) -> None:
    coordinator, _store, clock, *_ = session_system
    session = _create(coordinator)
    clock.value += timedelta(minutes=16)

    with pytest.raises(StaleWorkbenchSession, match="expired"):
        coordinator.save("project-1", session.token, _receipt())

    recovered = coordinator.inspect("project-1", session.token)
    assert recovered.state == "ready"
    assert recovered.workbench_output_revision is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"project_input_revision": "analysis-2"}, "project input"),
        ({"clip_input_revision": "analysis-2"}, "clip input"),
        ({"input_fingerprint": "changed"}, "fingerprint"),
        ({"workflow": "pure_rotation"}, "workflow"),
        ({"trajectory_job_id": "job-2"}, "trajectory run"),
        ({"trajectory_run_id": "run-2"}, "trajectory run"),
        ({"trajectory_output_revision": "trajectory-2"}, "trajectory output"),
        ({"trajectory_output_fingerprint": "changed"}, "trajectory output"),
    ],
)
def test_save_rejects_stale_input_workflow_run_or_output(
    session_system, change: dict[str, object], message: str
) -> None:
    coordinator, _store, _clock, current, _validated = session_system
    session = _create(coordinator)
    current[0] = replace(current[0], **change)

    with pytest.raises(StaleWorkbenchSession, match=message):
        coordinator.save("project-1", session.token, _receipt())


def test_create_requires_server_capability_and_successful_current_output(
    session_system,
) -> None:
    coordinator, _store, _clock, current, _validated = session_system
    current[0] = replace(current[0], can_open_workbench=False)

    with pytest.raises(WorkbenchPermissionDenied):
        _create(coordinator)

    current[0] = replace(
        current[0], can_open_workbench=True, trajectory_output_revision=""
    )
    with pytest.raises(StaleWorkbenchSession, match="trajectory output"):
        _create(coordinator)


def test_save_requires_explicit_permission(session_system) -> None:
    coordinator, _store, _clock, current, _validated = session_system
    current[0] = replace(current[0], save_permissions=())
    session = _create(coordinator)

    with pytest.raises(WorkbenchPermissionDenied, match="save"):
        coordinator.save("project-1", session.token, _receipt())


def test_validated_save_publishes_one_immutable_output_and_replay_fails(
    session_system,
) -> None:
    coordinator, _store, _clock, _current, validated = session_system
    session = _create(coordinator)

    saved = coordinator.save("project-1", session.token, _receipt())

    assert saved.state == "saved"
    assert saved.workbench_output_revision == "workbench-output-1"
    output_dir = coordinator.outputs_root / "project-1/workbench_outputs/workbench-output-1"
    manifest = json.loads((output_dir / "workbench_output_manifest.json").read_text())
    assert manifest["project_id"] == "project-1"
    assert manifest["clip_id"] == "clip-1"
    assert manifest["operation_id"] == saved.operation_id
    assert manifest["source_output_revision"] == "manual-track-7"
    assert len(validated) == 1
    with pytest.raises(ReplayedWorkbenchSave):
        coordinator.save("project-1", session.token, _receipt())
    assert len(validated) == 1


def test_retry_recovers_output_published_before_session_record_save(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, store, _clock, _current, _validated = session_system
    session = _create(coordinator)
    original_update = store.update
    failed = [False]

    def fail_saved_record(project_id, token, *, expected_revision, mutate):
        current = store.load(project_id, token)
        candidate = mutate(current)
        if candidate.state == "saved" and not failed[0]:
            failed[0] = True
            raise OSError("session record publication interrupted")
        return original_update(
            project_id,
            token,
            expected_revision=expected_revision,
            mutate=mutate,
        )

    monkeypatch.setattr(store, "update", fail_saved_record)
    with pytest.raises(OSError, match="session record publication interrupted"):
        coordinator.save("project-1", session.token, _receipt())

    pending = store.load("project-1", session.token)
    assert pending.state == "pending_save"
    assert pending.pending_output_revision == "workbench-output-1"
    output = (
        coordinator.outputs_root
        / "project-1/workbench_outputs/workbench-output-1/workbench_output_manifest.json"
    )
    before = output.read_bytes()
    saved = coordinator.save("project-1", session.token, _receipt())
    assert saved.state == "saved"
    assert saved.operation_id == pending.operation_id
    assert output.read_bytes() == before


def test_pending_save_rejects_different_validated_receipt(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, store, _clock, _current, _validated = session_system
    session = _create(coordinator)
    original_update = store.update

    def always_fail_saved(project_id, token, *, expected_revision, mutate):
        current = store.load(project_id, token)
        if mutate(current).state == "saved":
            raise OSError("interrupt final session write")
        return original_update(
            project_id,
            token,
            expected_revision=expected_revision,
            mutate=mutate,
        )

    monkeypatch.setattr(store, "update", always_fail_saved)
    with pytest.raises(OSError):
        coordinator.save("project-1", session.token, _receipt())

    with pytest.raises(InvalidWorkbenchOutput, match="pending save"):
        coordinator.save(
            "project-1",
            session.token,
            _receipt(source_output_fingerprint="e" * 64),
        )


def test_abandon_preserves_pending_save_for_recovery(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, store, _clock, _current, _validated = session_system
    session = _create(coordinator)
    original_update = store.update

    def interrupt_final(project_id, token, *, expected_revision, mutate):
        current = store.load(project_id, token)
        if mutate(current).state == "saved":
            raise OSError("interrupt final save")
        return original_update(
            project_id, token, expected_revision=expected_revision, mutate=mutate
        )

    monkeypatch.setattr(store, "update", interrupt_final)
    with pytest.raises(OSError):
        coordinator.save("project-1", session.token, _receipt())
    pending = coordinator.abandon("project-1", session.token)
    assert pending.state == "pending_save"
    assert pending.pending_output_revision == "workbench-output-1"

def test_invalid_adapter_output_never_publishes_or_marks_saved(session_system) -> None:
    coordinator, _store, _clock, _current, _validated = session_system
    session = _create(coordinator)

    with pytest.raises(InvalidWorkbenchOutput):
        coordinator.save("project-1", session.token, _receipt(ok=False))

    assert coordinator.inspect("project-1", session.token).state == "editing"
    assert not (coordinator.outputs_root / "project-1/workbench_outputs").exists()


def test_output_publication_rejects_temp_directory_outside_its_owned_parent(
    session_system, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, *_ = session_system
    session = _create(coordinator)
    sentinel_dir = tmp_path / "cwd-sentinel"
    sentinel_dir.mkdir()
    sentinel = sentinel_dir / "do-not-touch.txt"
    sentinel.write_text("owned by caller", encoding="utf-8")

    monkeypatch.setattr(
        "cadscene.projects.workbench_sessions.tempfile.mkdtemp",
        lambda **_kwargs: str(sentinel_dir),
    )
    monkeypatch.setattr(
        "cadscene.projects.workbench_sessions.os.replace",
        lambda _source, _destination: None,
    )

    with pytest.raises(InvalidWorkbenchOutput, match="temporary output directory"):
        coordinator.save("project-1", session.token, _receipt())

    assert sentinel.read_text(encoding="utf-8") == "owned by caller"
    assert sorted(path.name for path in sentinel_dir.iterdir()) == [
        "do-not-touch.txt"
    ]


def test_abandon_unsaved_editing_returns_ready_but_preserves_saved_revision(
    session_system,
) -> None:
    coordinator, *_ = session_system
    editing = _create(coordinator)

    ready = coordinator.abandon("project-1", editing.token)
    assert ready.state == "ready"
    assert ready.workbench_output_revision is None

    second = _create(coordinator)
    saved = coordinator.save("project-1", second.token, _receipt())
    closed = coordinator.abandon("project-1", second.token)
    assert closed.state == "saved"
    assert closed.workbench_output_revision == saved.workbench_output_revision


def test_atomic_store_uses_revision_and_never_leaves_partial_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MutableClock()
    store = AtomicWorkbenchSessionStore(tmp_path / "projects", now=clock)
    current = [_context()]
    coordinator = WorkbenchSessionCoordinator(
        store=store,
        outputs_root=tmp_path / "projects",
        resolve_context=lambda _project_id, _clip_id: current[0],
        validate_output=lambda _session, receipt: receipt,
        now=clock,
        token_factory=lambda: "atomic-record-token-" + "y" * 32,
        revision_factory=lambda: "unused",
    )
    original_replace = __import__("os").replace
    replace_calls = 0

    session = _create(coordinator)
    assert session.schema_version == "1.0"
    assert session.revision == 0
    assert session.updated_at

    def interrupted(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise OSError("simulated interruption before atomic replace")
        return original_replace(source, destination)

    monkeypatch.setattr("cadscene.projects.workbench_sessions.os.replace", interrupted)
    with pytest.raises(OSError, match="simulated interruption"):
        store.update(
            "project-1",
            session.token,
            expected_revision=session.revision,
            mutate=lambda value: replace(value, operation_id="op-failed"),
        )

    persisted = store.load("project-1", session.token)
    assert persisted.revision == 0
    assert persisted.operation_id == session.operation_id
    record_path = store.path_for("project-1", session.token)
    json.loads(record_path.read_text(encoding="utf-8"))
    assert not list(record_path.parent.glob("*.tmp"))


def _project_api_with_workbench(tmp_path: Path, *, workflow: str = "sfm_only"):
    projects_root = tmp_path / "projects"
    runs_root = tmp_path / "runs"
    repositories = project_repositories(projects_root)
    repositories.create_project("project-1", updated_at="2026-08-04T08:00:00Z")
    source = tmp_path / "source.mp4"
    physical_clip = tmp_path / "clip-1.mp4"
    source.write_bytes(b"source-video")
    physical_clip.write_bytes(b"physical-clip")
    project = repositories.project.load("project-1")
    repositories.project.update(
        "project-1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            register_analysis_revision(
                value, "analysis-1", operation_id="analysis-operation"
            ),
            source_assets={"video_path": str(source)},
            project_state="ready",
        ),
    )
    clip = ClipDefinition.from_analysis(
        {
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 100,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "recommended_workflow": workflow,
            "physical_mp4_path": str(physical_clip),
            "input_snapshot": {
                "request_key": "analysis-request-1",
                "video": {"path": str(source), "sha256": "v" * 64},
                "cad": None,
                "srt": None,
                "analysis_artifact": {
                    "path": str(tmp_path / "analysis.json"),
                    "artifact_id": "analysis-artifact-1",
                },
            },
        }
    )
    clips = repositories.clips.load("project-1")
    clips = repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(clip,)
        ),
    )
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: "2026-08-04T08:00:00Z",
    )
    project = repositories.project.load("project-1")
    job = service._new_job(
        "project-1",
        clip,
        job_type="trajectory",
        resource_class="heavy_compute",
        adapter_name=workflow,
        adapter_version="1",
        exclusive_key="trajectory:project-1:clip-1",
        dependency_ids=(),
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=clips.revision,
    )
    trajectory = tmp_path / "trajectory.json"
    trajectory.write_text('{"poses":[{"frame_index":0}]}', encoding="utf-8")
    job = replace(
        job,
        status="success",
        stage="success",
        output_revision="trajectory-output-1",
        output_fingerprint="a" * 64,
        output_validated=True,
        validated_input_fingerprint=job.input_fingerprint,
        published_outputs={"trajectory": str(trajectory)},
    )
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=(job.to_dict(),)),
    )
    clock = MutableClock()
    token_counter = [0]
    workbench = ProjectWorkbenchService(
        repositories=repositories,
        project_service=service,
        session_store=AtomicWorkbenchSessionStore(projects_root, now=clock),
        projects_root=projects_root,
        viewer_runs_root=runs_root,
        now=clock,
        token_factory=lambda: (
            token_counter.__setitem__(0, token_counter[0] + 1)
            or f"api-session-{token_counter[0]}-" + "z" * 32
        ),
        revision_factory=lambda: "workbench-output-api-1",
    )
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(projects_root),
        now=lambda: "2026-08-04T08:00:00Z",
        workbench=workbench,
    )
    return api, repositories, runs_root, job


def test_snapshot_exposes_server_derived_workbench_capability_and_state(
    tmp_path: Path,
) -> None:
    api, _repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")

    clip = snapshot.body["clips"][0]
    assert clip["capabilities"]["can_open_workbench"] is True
    assert clip["workbench"]["state"] == "ready"
    assert clip["workbench"]["workbench_output_revision"] is None


def test_snapshot_rejects_historical_success_from_an_old_clip_input(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    historical = replace(job, input_revision="analysis-old")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=(historical.to_dict(),)),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    assert snapshot.body["clips"][0]["capabilities"]["can_open_workbench"] is False
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 403


def test_expired_editing_changes_snapshot_etag_and_never_returns_stale_304(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    editing = api.handle("GET", "/api/projects/project-1/snapshot")
    assert editing.body["clips"][0]["workbench"]["state"] == "editing"
    api.workbench.now.value += timedelta(minutes=31)

    expired = api.handle(
        "GET",
        "/api/projects/project-1/snapshot",
        headers={"If-None-Match": editing.headers["ETag"]},
    )

    assert expired.status == 200
    assert expired.headers["ETag"] != editing.headers["ETag"]
    assert expired.body["clips"][0]["workbench"]["state"] == "ready"


def test_existing_workbench_reference_never_bypasses_real_input_change(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    token = opened.body["token"]
    clips = repositories.clips.load("project-1")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            clips=(replace(value.clips[0], manual_definition={"changed": True}),),
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    assert snapshot.body["clips"][0]["capabilities"]["can_open_workbench"] is False
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"keyframes":[{"frame":0}]}', encoding="utf-8")
    stale = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )
    assert stale.status == 409
    assert stale.body["error"] == "stale_workbench_session"


def test_workbench_http_create_bootstrap_save_and_replay_fail_closed(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, job = _project_api_with_workbench(tmp_path)
    clips_revision = repositories.clips.load("project-1").revision
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": clips_revision,
            "return_to": (
                "/apps/project_workspace/?projectId=project-1&focusClip=clip-1"
            ),
        },
    )

    assert opened.status == 201
    token = opened.body["token"]
    assert token in opened.body["workbench_url"]
    assert "runId=clip-1" in opened.body["workbench_url"]
    assert repositories.clips.load("project-1").revision == clips_revision + 1
    bootstrap = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{token}"
    )
    assert bootstrap.status == 200
    assert bootstrap.body["clip_id"] == "clip-1"
    assert bootstrap.body["trajectory_job_id"] == job.job_id
    assert bootstrap.body["state"] == "editing"

    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text(
        json.dumps({"fps": 25, "keyframes": [{"frame": 0}]}),
        encoding="utf-8",
    )
    expected_revision = repositories.clips.load("project-1").revision
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/save",
        json_body={
            "expected_revision": expected_revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )
    assert saved.status == 200
    assert saved.body["state"] == "saved"
    assert saved.body["workbench_output_revision"] == "workbench-output-api-1"

    replay = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )
    assert replay.status == 409
    assert replay.body["error"] == "workbench_save_replayed"


def test_workbench_http_close_recovers_editing_to_ready_and_checks_revision(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    token = opened.body["token"]

    conflict = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/close",
        json_body={"expected_revision": 0},
    )
    assert conflict.status == 409
    closed = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/close",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision
        },
    )
    assert closed.status == 200
    assert closed.body["state"] == "ready"
    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    assert snapshot.body["clips"][0]["workbench"]["state"] == "ready"


def test_workbench_save_rejects_client_path_outside_bound_run(tmp_path: Path) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    outside = tmp_path / "outside.json"
    outside.write_text('{"keyframes":[]}', encoding="utf-8")

    rejected = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(outside)},
        },
    )

    assert rejected.status == 400
    assert "bound workbench output" in rejected.body["error"]
    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    assert snapshot.body["clips"][0]["workbench"]["state"] == "editing"


def test_retry_repairs_saved_session_when_clip_reference_publication_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    token = opened.body["token"]
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"keyframes":[{"frame":0}]}', encoding="utf-8")
    original_update = repositories.clips.update
    failed = [False]

    def interrupt_saved_reference(project_id, *, expected_revision, mutate):
        current = repositories.clips.load(project_id)
        candidate = mutate(current)
        reference = candidate.clips[0].references[-1]
        if reference.value.get("status") == "saved" and not failed[0]:
            failed[0] = True
            raise OSError("clip reference publication interrupted")
        return original_update(
            project_id, expected_revision=expected_revision, mutate=mutate
        )

    monkeypatch.setattr(repositories.clips, "update", interrupt_saved_reference)
    with pytest.raises(OSError, match="clip reference publication interrupted"):
        api.workbench.save(
            "project-1",
            token,
            {"ok": True, "path": str(output)},
            expected_clips_revision=repositories.clips.load("project-1").revision,
        )

    session = api.workbench.inspect("project-1", token)
    assert session.state == "saved"
    repaired = api.workbench.save(
        "project-1",
        token,
        {"ok": True, "path": str(output)},
        expected_clips_revision=repositories.clips.load("project-1").revision,
    )
    assert repaired.state == "saved"
    reference = repositories.clips.load("project-1").clips[0].references[-1]
    assert reference.operation_id == session.operation_id
    assert reference.value["workbench_output_revision"] == session.workbench_output_revision
    with pytest.raises(ReplayedWorkbenchSave):
        api.workbench.save(
            "project-1",
            token,
            {"ok": True, "path": str(output)},
            expected_clips_revision=repositories.clips.load("project-1").revision,
        )


def test_pure_rotation_save_selects_and_validates_server_run_output(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(
        tmp_path, workflow="pure_rotation"
    )
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    run = runs_root / "project-1/clip-1"
    base = run / "03_pure_rotation_placement/camera_track_cad_base.json"
    corrected = run / "04_pure_rotation_corrections/camera_track_corrected.json"
    for path, frame in ((base, 0), (corrected, 7)):
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "trajectory_mode": "pure_rotation_manual_calibrated",
                    "poses": [
                        {
                            "decoded_frame_index": frame,
                            "rotation_cad_from_camera": [
                                [1, 0, 0],
                                [0, 1, 0],
                                [0, 0, 1],
                            ],
                            "camera_center_web": [1, 2, 3],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    malicious = tmp_path / "attacker.json"
    malicious.write_text('{"poses":[]}', encoding="utf-8")

    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {
                "ok": True,
                "kind": "pure_rotation_calibration",
                "path": str(malicious),
            },
        },
    )

    assert saved.status == 200
    revision = saved.body["workbench_output_revision"]
    manifest = json.loads(
        (
            tmp_path
            / f"projects/project-1/workbench_outputs/{revision}/workbench_output_manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["artifacts"]["camera_track"].endswith(
        "04_pure_rotation_corrections/camera_track_corrected.json"
    )
    assert manifest["source_output_revision"].startswith("pure-track:")


def test_second_live_session_and_old_session_mutations_fail_closed(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    create_path = "/api/projects/project-1/clips/clip-1/workbench-sessions"
    first = api.handle(
        "POST",
        create_path,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    blocked = api.handle(
        "POST",
        create_path,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert blocked.status in {403, 409}

    api.workbench.now.value += timedelta(minutes=31)
    second = api.handle(
        "POST",
        create_path,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert second.status == 201
    stale_close = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{first.body['token']}/close",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision
        },
    )
    assert stale_close.status == 409
    current = repositories.clips.load("project-1").clips[0].references[-1]
    assert current.value["session_token_hash"] == sha256(
        second.body["token"].encode("utf-8")
    ).hexdigest()


def test_pending_save_blocks_replacement_even_after_editing_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    create_path = "/api/projects/project-1/clips/clip-1/workbench-sessions"
    opened = api.handle(
        "POST",
        create_path,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text('{"keyframes":[{"frame":0}]}', encoding="utf-8")
    store = api.workbench.coordinator.store
    original_update = store.update

    def interrupt_final(project_id, token, *, expected_revision, mutate):
        current = store.load(project_id, token)
        if mutate(current).state == "saved":
            raise OSError("interrupt saved record")
        return original_update(
            project_id, token, expected_revision=expected_revision, mutate=mutate
        )

    monkeypatch.setattr(store, "update", interrupt_final)
    with pytest.raises(OSError):
        api.workbench.save(
            "project-1",
            opened.body["token"],
            {"ok": True, "path": str(output)},
            expected_clips_revision=repositories.clips.load("project-1").revision,
        )
    api.workbench.now.value += timedelta(minutes=31)

    blocked = api.handle(
        "POST",
        create_path,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert blocked.status == 403
    assert "pending workbench save" in blocked.body["error"]
