from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
from cadscene.projects.service import _validate_clip_export_outputs
from cadscene.projects.scene_bridge_runner import (
    SceneBridgeInputs,
    validate_scene_bridge_candidate,
)
from cadscene.projects.uploads import ValidatedUploadStore
from cadscene.projects.workflow_adapters import default_workflow_adapters
from cadscene.workflow.data_import import slugify_dataset_name


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
        source = tmp_path / "validated-camera-track.json"
        source.write_text(
            json.dumps(
                {
                    "receipt_fingerprint": receipt["source_output_fingerprint"],
                    "poses": [{"frame": 0}],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        source_bytes = source.read_bytes()
        return {
            "source_output_revision": str(receipt["source_output_revision"]),
            "source_output_fingerprint": sha256(source_bytes).hexdigest(),
            "source_path": str(source),
            "source_bytes": source_bytes,
            "source_artifact_name": source.name,
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


def _manual_track() -> dict[str, object]:
    return {
        "fps": 25.0,
        "keyframes": [
            {
                "frame": 0,
                "time": 0.0,
                "camera": {
                    "x": 1.0, "y": 2.0, "z": 3.0,
                    "yaw": 0.0, "pitch": -45.0, "roll": 0.0, "fov": 70.0,
                },
            }
        ],
    }


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


def test_expired_pending_save_recovers_already_published_output(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, store, clock, _current, _validated = session_system
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
            project_id, token, expected_revision=expected_revision, mutate=mutate
        )

    monkeypatch.setattr(store, "update", fail_saved_record)
    with pytest.raises(OSError, match="session record publication interrupted"):
        coordinator.save("project-1", session.token, _receipt())
    monkeypatch.setattr(store, "update", original_update)
    clock.value += timedelta(minutes=16)

    saved = coordinator.save("project-1", session.token, _receipt())

    assert saved.state == "saved"
    assert saved.workbench_output_revision == "workbench-output-1"


@pytest.mark.parametrize(
    "wrong_receipt",
    [
        _receipt(ok=False),
        _receipt(source_output_revision="different-manual-track"),
    ],
)
def test_expired_pending_with_published_output_rejects_different_receipt(
    session_system,
    monkeypatch: pytest.MonkeyPatch,
    wrong_receipt: dict[str, object],
) -> None:
    coordinator, store, clock, _current, _validated = session_system
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
            project_id, token, expected_revision=expected_revision, mutate=mutate
        )

    monkeypatch.setattr(store, "update", fail_saved_record)
    with pytest.raises(OSError, match="session record publication interrupted"):
        coordinator.save("project-1", session.token, _receipt())
    monkeypatch.setattr(store, "update", original_update)
    clock.value += timedelta(minutes=16)

    with pytest.raises(InvalidWorkbenchOutput, match="pending save"):
        coordinator.save("project-1", session.token, wrong_receipt)

    assert store.load("project-1", session.token).state == "pending_save"


def test_expired_pending_save_revalidates_and_publishes_missing_output(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator, store, clock, _current, _validated = session_system
    session = _create(coordinator)
    original_publish = coordinator._publish_output
    failed = [False]

    def fail_first_publish(*args, **kwargs):
        if not failed[0]:
            failed[0] = True
            raise OSError("output publication interrupted")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(coordinator, "_publish_output", fail_first_publish)
    with pytest.raises(OSError, match="output publication interrupted"):
        coordinator.save("project-1", session.token, _receipt())
    assert store.load("project-1", session.token).state == "pending_save"
    clock.value += timedelta(minutes=16)

    saved = coordinator.save("project-1", session.token, _receipt())

    assert saved.state == "saved"
    assert saved.workbench_output_revision == "workbench-output-1"


def test_retry_uses_published_immutable_output_after_mutable_source_changes(
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

    mutable_source = coordinator.outputs_root.parent / "validated-camera-track.json"
    mutable_source.write_text('{"new":"legal later save"}', encoding="utf-8")

    def validate_changed_source(_session, _receipt_payload):
        return {
            "source_output_revision": "manual-track-8",
            "source_output_fingerprint": sha256(mutable_source.read_bytes()).hexdigest(),
            "source_path": str(mutable_source),
            "source_artifact_name": mutable_source.name,
        }

    coordinator.validate_output = validate_changed_source
    saved = coordinator.save("project-1", session.token, _receipt())

    assert saved.state == "saved"
    assert saved.workbench_output_revision == "workbench-output-1"
    immutable = (
        coordinator.outputs_root
        / "project-1/workbench_outputs/workbench-output-1/artifacts/validated-camera-track.json"
    )
    assert immutable.read_text(encoding="utf-8") != mutable_source.read_text(encoding="utf-8")


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

    published = (
        coordinator.outputs_root
        / "project-1/workbench_outputs/workbench-output-1"
    )
    published.rename(published.with_name("unpublished-interrupted-output"))

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


def test_session_record_atomic_replace_fsyncs_parent_directory(
    session_system, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cadscene.projects.workbench_sessions as sessions_module

    coordinator, store, *_ = session_system
    synced: list[Path] = []
    monkeypatch.setattr(
        sessions_module,
        "_fsync_directory",
        lambda path: synced.append(Path(path).resolve(strict=False)),
    )

    session = _create(coordinator)

    assert store.path_for("project-1", session.token).parent.resolve() in synced


def _project_api_with_workbench(tmp_path: Path, *, workflow: str = "sfm_only"):
    projects_root = tmp_path / "projects"
    runs_root = tmp_path / "runs"
    repositories = project_repositories(projects_root)
    repositories.create_project("project-1", updated_at="2026-08-04T08:00:00Z")
    source = tmp_path / "source.mp4"
    physical_clip = tmp_path / "clip-1.mp4"
    cad_dataset = tmp_path / "cad-dataset"
    source.write_bytes(b"source-video")
    physical_clip.write_bytes(b"physical-clip")
    cad_dataset.mkdir()
    (cad_dataset / "design.json").write_text(
        json.dumps({"entities": [{"type": "line"}]}), encoding="utf-8"
    )
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
                "cad": {
                    "path": str(tmp_path / "source.dxf"),
                    "sha256": "c" * 64,
                    "dataset_id": "cad-test",
                    "dataset_path": str(cad_dataset),
                },
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
    adapter = service.adapters.for_workflow(workflow)
    project = repositories.project.load("project-1")
    job = service._new_job(
        "project-1",
        clip,
        job_type="trajectory",
        resource_class="heavy_compute",
        adapter_name=adapter.name,
        adapter_version=adapter.version,
        exclusive_key="trajectory:project-1:clip-1",
        dependency_ids=(),
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=clips.revision,
    )
    trajectory = (
        Path(job.attempts[-1].directory)
        / "project-1/clip-1"
        / (
            "02_pure_rotation/camera_rotation_raw.json"
            if workflow == "pure_rotation"
            else "02_sfm/camera_trajectory.json"
        )
    )
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_text(
        json.dumps(
            {
                "poses": [{"frame_index": 0}],
                **(
                    {"trajectory_mode": "pure_rotation_only"}
                    if workflow == "pure_rotation"
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )
    job = replace(
        job,
        status="success",
        stage="success",
        output_revision="trajectory-output-1",
        output_fingerprint=sha256(trajectory.read_bytes()).hexdigest(),
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


def _add_same_scene_adjacent_clips(
    api: ProjectApi,
    repositories,
    tmp_path: Path,
) -> None:
    current = repositories.clips.load("project-1")
    first = current.clips[0]
    clips = []
    for index in range(1, 4):
        physical = tmp_path / f"clip-{index}.mp4"
        physical.write_bytes(f"physical-{index}".encode("ascii"))
        frame_map = tmp_path / f"clip-{index}-frame-map.json"
        frame_map.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "interval_semantics": "half_open",
                    "source_time_base": {"numerator": 1, "denominator": 1000},
                    "clips": [
                        {
                            "clip_id": f"clip-{index}",
                            "source_start_pts": (index - 1) * 100,
                            "source_end_pts_exclusive": index * 100,
                            "frames": [
                                {
                                    "output_frame_ordinal": ordinal,
                                    "source_decoded_frame_ordinal": (
                                        (index - 1) * 4 + ordinal
                                    ),
                                    "source_pts": (
                                        (index - 1) * 100 + ordinal * 25
                                    ),
                                }
                                for ordinal in range(4)
                            ],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        analysis = {
            **dict(first.analysis),
            "clip_id": f"clip-{index}",
            "scene_index": 1,
            "segment_index": index,
            "render_order": index - 1,
            "source_start_pts": (index - 1) * 100,
            "source_end_pts_exclusive": index * 100,
            "physical_mp4_path": str(physical),
            "frame_map_path": str(frame_map),
        }
        clips.append(
            ClipDefinition.from_analysis(
                analysis,
                generated_display_name=f"场景 01 · 第 {index} 段",
            )
        )
    published = repositories.clips.update(
        "project-1",
        expected_revision=current.revision,
        mutate=lambda value: replace(value, clips=tuple(clips)),
    )
    project = repositories.project.load("project-1")
    adapter = api.service.adapters.for_workflow("sfm_only")
    jobs = list(repositories.jobs.load("project-1").jobs)
    for clip in clips[1:]:
        job = api.service._new_job(
            "project-1",
            clip,
            job_type="trajectory",
            resource_class="heavy_compute",
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            exclusive_key=f"trajectory:project-1:{clip.clip_id}",
            dependency_ids=(),
            project_assets=project.source_assets,
            project_revision=project.revision,
            clips_revision=published.revision,
        )
        trajectory = (
            Path(job.attempts[-1].directory)
            / "project-1"
            / clip.clip_id
            / "02_sfm/camera_trajectory.json"
        )
        trajectory.parent.mkdir(parents=True, exist_ok=True)
        trajectory.write_text(
            json.dumps(
                {
                    "fps": 40.0,
                    "poses": [
                        {"frame_index": frame, "registered": True}
                        for frame in (0, 3)
                    ],
                }
            ),
            encoding="utf-8",
        )
        sparse = trajectory.with_name("sparse_points.ply")
        sparse.write_bytes(b"ply")
        jobs.append(
            replace(
                job,
                status="success",
                stage="success",
                output_revision=f"trajectory-{clip.clip_id}",
                output_fingerprint=sha256(trajectory.read_bytes()).hexdigest(),
                output_validated=True,
                validated_input_fingerprint=job.input_fingerprint,
                published_outputs={"trajectory": str(trajectory)},
            ).to_dict()
        )
    manifest = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=manifest.revision,
        mutate=lambda value: replace(value, jobs=tuple(jobs)),
    )
    api.service.queue.merge_restored(
        jobs,
        project_id="project-1",
        queue_order=[str(item["job_id"]) for item in jobs],
    )


def _save_completed_sfm_route(
    api: ProjectApi,
    repositories,
    runs_root: Path,
    *,
    clip_id: str = "clip-2",
) -> dict[str, object]:
    opened = api.handle(
        "POST",
        f"/api/projects/project-1/clips/{clip_id}/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    track = {
        "version": 1,
        "fps": 25.0,
        "keyframes": [
            {
                "frame": frame,
                "time": frame / 25.0,
                "source": "manual_anchor",
                "camera": {
                    "x": float(frame),
                    "y": 2.0,
                    "z": 3.0,
                    "yaw": 4.0,
                    "pitch": -20.0,
                    "roll": 0.0,
                    "fov": 70.0,
                },
            }
            for frame in (0, 3)
        ],
    }
    manual = (
        runs_root
        / f"project-1-{clip_id}"
        / clip_id
        / "01_keyframes/camera_track_manual.json"
    )
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(json.dumps(track), encoding="utf-8")
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(manual)},
        },
    )
    assert saved.status == 200
    return dict(saved.body)


def _scene_bridge_result(api: ProjectApi, bridge) -> object:
    request_path = (
        api.service.projects_root
        / bridge.project_id
        / "scene_bridge_requests"
        / f"{bridge.operation_id}.json"
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))
    identity = request["runner_identity"]
    root = Path(bridge.attempts[-1].directory) / "candidate"
    artifacts = {
        "camera_track": "camera_track_seed.json",
        "alignment": "core_alignment/03_alignment/alignment.json",
        "camera_path": "core_alignment/03_alignment/sfm_camera_path.csv",
        "viewer_scene": "core_alignment/05_viewer_scene/sfm_viewer_scene.json",
    }
    for name, relative in artifacts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if name == "camera_track":
            path.write_text(
                json.dumps(
                    {
                        "keyframes": [
                            {"frame": frame, "source": "scene_overlap_anchor"}
                            for frame in (0, 3)
                        ]
                    }
                ),
                encoding="utf-8",
            )
        else:
            path.write_text(f"{name}\n", encoding="utf-8")
    artifact_identity = {
        name: {
            "path": relative,
            "sha256": sha256((root / relative).read_bytes()).hexdigest(),
            "size_bytes": (root / relative).stat().st_size,
        }
        for name, relative in artifacts.items()
    }
    proof = {
        "identity": identity,
        "solve_interval": {
            "source_start_pts": 96,
            "source_end_pts_exclusive": 204,
            "core_start_pts": 100,
            "core_end_pts_exclusive": 200,
            "time_base": {"numerator": 1, "denominator": 25},
        },
        "anchor_source_pts": [96, 100],
        "artifacts": artifact_identity,
    }
    fingerprint = sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    revision = f"bridge-{fingerprint[:16]}"
    (root / "scene_bridge_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "awaiting_route_refinement",
                **proof,
                "output_revision": revision,
                "output_fingerprint": fingerprint,
            }
        ),
        encoding="utf-8",
    )
    result = validate_scene_bridge_candidate(root, identity)
    assert result.status == "success"
    return result


def test_scene_bridge_request_queues_current_target_trajectory_dependency(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    _save_completed_sfm_route(api, repositories, runs_root)

    response = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/scene-bridges",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
        },
    )

    assert response.status == 202
    assert response.body["state"] == "scene_bridge_queued"
    assert response.body["target_clip_id"] == "clip-3"
    bridge = api.service.queue.get(str(response.body["job_id"]))
    assert bridge.job_type == "scene_bridge"
    assert bridge.adapter_name == "scene_bridge"
    assert len(bridge.depends_on_job_ids) == 1
    dependency = api.service.queue.get(bridge.depends_on_job_ids[0])
    assert dependency.job_type == "trajectory"
    assert dependency.clip_id == "clip-3"
    assert api.service._current_input_fingerprint(bridge) == bridge.input_fingerprint


def test_repeated_current_scene_bridge_request_is_idempotent(tmp_path: Path) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    _save_completed_sfm_route(api, repositories, runs_root)

    def enqueue():
        return api.handle(
            "POST",
            "/api/projects/project-1/clips/clip-2/scene-bridges",
            json_body={
                "direction": "down",
                "expected_revision": repositories.clips.load("project-1").revision,
                "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            },
        )

    first = enqueue()
    second = enqueue()

    assert first.status == second.status == 202
    assert second.body["job_id"] == first.body["job_id"]
    requests = list(
        (
            api.service.projects_root
            / "project-1"
            / "scene_bridge_requests"
        ).glob("*.json")
    )
    assert len(requests) == 1


def test_scene_bridge_execution_plan_binds_saved_route_and_current_core_inputs(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    saved = _save_completed_sfm_route(api, repositories, runs_root)
    response = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/scene-bridges",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
        },
    )
    bridge = api.service.queue.get(str(response.body["job_id"]))

    plan = api.service._build_job_execution_plan_locked(bridge)

    assert len(plan.commands) == 1
    command = plan.commands[0]
    assert command[1:3] == ("-m", "cadscene.cli.run_scene_bridge")
    inputs = SceneBridgeInputs.from_json(Path(command[command.index("--inputs") + 1]))
    assert inputs.identity["operation_id"] == bridge.operation_id
    assert inputs.identity["source_workbench_output_revision"] == saved[
        "workbench_output_revision"
    ]
    assert inputs.source_manual_track_path.is_file()
    assert inputs.source_core_frame_map_path.name.endswith("frame-map.json")
    assert inputs.target_core_frame_map_path.name.endswith("frame-map.json")
    assert inputs.target_core_trajectory_path.is_file()
    assert inputs.target_core_sparse_ply_path.is_file()


def test_scene_bridge_success_publishes_immutable_route_refinement(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    _save_completed_sfm_route(api, repositories, runs_root)
    response = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/scene-bridges",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
        },
    )
    running = api.service.queue.claim_next_unstarted()
    assert running is not None and running.job_id == response.body["job_id"]
    result = _scene_bridge_result(api, running)
    attempt = running.attempts[-1]

    finished = api.service.finish_job(
        "project-1",
        running.job_id,
        result,
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "success"
    assert finished.validation_proof == result.validation_proof
    target = repositories.clips.load("project-1").clips[2]
    active = next(
        reference
        for reference in target.references
        if reference.key == "scene_bridge:clip-3"
    )
    assert active.value["status"] == "awaiting_route_refinement"
    assert active.value["source_clip_id"] == "clip-2"
    assert active.value["bridge_revision"] == result.output_revision
    published_root = (
        api.service.projects_root
        / "project-1/scene_bridges/clip-3"
        / str(result.output_revision)
    )
    assert Path(active.value["manifest_path"]) == (
        published_root / "scene_bridge_manifest.json"
    )
    assert (published_root / "camera_track_seed.json").is_file()
    assert not (published_root / "core_alignment/04_quality").exists()


def test_scene_bridge_does_not_publish_when_source_route_changes(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    _save_completed_sfm_route(api, repositories, runs_root)
    response = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/scene-bridges",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
        },
    )
    running = api.service.queue.claim_next_unstarted()
    assert running is not None and running.job_id == response.body["job_id"]
    result = _scene_bridge_result(api, running)
    clips = repositories.clips.load("project-1")
    source = clips.clips[1]
    changed = tuple(
        replace(
            reference,
            value={**reference.value, "workbench_output_fingerprint": "f" * 64},
        )
        if reference.key == "workbench:clip-2"
        else reference
        for reference in source.references
    )
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            clips=(value.clips[0], replace(source, references=changed), value.clips[2]),
        ),
    )
    attempt = running.attempts[-1]

    finished = api.service.finish_job(
        "project-1",
        running.job_id,
        result,
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    assert finished.status == "stale_input"
    target = repositories.clips.load("project-1").clips[2]
    assert not any(reference.key == "scene_bridge:clip-3" for reference in target.references)
    assert not (
        api.service.projects_root
        / "project-1/scene_bridges/clip-3"
        / str(result.output_revision)
    ).exists()


def test_saved_route_can_push_boundary_pose_to_previous_and_next_clip(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    track = {
        "version": 1,
        "fps": 25.0,
        "keyframes": [
            {
                "frame": 0,
                "time": 0.0,
                "source": "manual_anchor",
                "camera": {
                    "x": 10.0,
                    "y": 20.0,
                    "z": 30.0,
                    "yaw": 40.0,
                    "pitch": -20.0,
                    "roll": 0.0,
                    "fov": 70.0,
                },
            },
            {
                "frame": 3,
                "time": 0.12,
                "source": "manual_anchor",
                "camera": {
                    "x": 110.0,
                    "y": 120.0,
                    "z": 130.0,
                    "yaw": 140.0,
                    "pitch": -10.0,
                    "roll": 1.0,
                    "fov": 72.0,
                },
            },
        ],
    }
    manual = runs_root / "project-1-clip-2/clip-2/01_keyframes/camera_track_manual.json"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(json.dumps(track), encoding="utf-8")
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(manual)},
        },
    )
    assert saved.status == 200

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    source = next(
        item for item in snapshot.body["clips"] if item["clip_id"] == "clip-2"
    )
    assert source["capabilities"]["locate_up_target_clip_id"] == "clip-1"
    assert source["capabilities"]["locate_down_target_clip_id"] == "clip-3"

    downward = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/locate-adjacent",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert downward.status == 201
    assert downward.body["target_clip_id"] == "clip-3"
    assert parse_qs(urlsplit(downward.body["workbench_url"]).query)["initialFrame"] == [
        "0"
    ]
    down_track = json.loads(
        (
            runs_root / "project-1-clip-3/clip-3/01_keyframes/camera_track_manual.json"
        ).read_text(encoding="utf-8")
    )
    assert down_track["keyframes"][0]["frame"] == 0
    assert down_track["keyframes"][0]["source"] == "scene_boundary_anchor"
    assert down_track["keyframes"][0]["camera"] == track["keyframes"][-1]["camera"]
    repeated_downward = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/locate-adjacent",
        json_body={
            "direction": "down",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert repeated_downward.status == 403
    assert (
        len(
            list(
                (api.service.projects_root / "project-1/workbench_seeds/clip-3").glob(
                    "*/workbench_seed_manifest.json"
                )
            )
        )
        == 1
    )

    upward = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/locate-adjacent",
        json_body={
            "direction": "up",
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert upward.status == 201
    assert upward.body["target_clip_id"] == "clip-1"
    assert parse_qs(urlsplit(upward.body["workbench_url"]).query)["initialFrame"] == [
        "3"
    ]
    up_track = json.loads(
        (
            runs_root / "project-1-clip-1/clip-1/01_keyframes/camera_track_manual.json"
        ).read_text(encoding="utf-8")
    )
    assert up_track["keyframes"][0]["frame"] == 3
    assert up_track["keyframes"][0]["camera"] == track["keyframes"][0]["camera"]
    seed_manifest = next(
        (api.service.projects_root / "project-1/workbench_seeds/clip-1").glob(
            "*/workbench_seed_manifest.json"
        )
    )
    seed = json.loads(seed_manifest.read_text(encoding="utf-8"))
    assert seed["source_clip_id"] == "clip-2"
    assert (
        seed["source_workbench_output_revision"]
        == saved.body["workbench_output_revision"]
    )


def test_adjacent_location_prepares_unexported_target_without_leaving_seed(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    current = repositories.clips.load("project-1")
    repositories.clips.update(
        "project-1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(
                replace(
                    clip,
                    analysis={
                        key: item
                        for key, item in clip.analysis.items()
                        if key not in {"physical_mp4_path", "frame_map_path"}
                    },
                )
                if clip.clip_id == "clip-3"
                else clip
                for clip in value.clips
            ),
        ),
    )
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    track = {
        "version": 1,
        "fps": 25.0,
        "keyframes": [
            {
                "frame": frame,
                "time": frame / 25.0,
                "source": "manual_anchor",
                "camera": {
                    "x": float(frame),
                    "y": 2.0,
                    "z": 3.0,
                    "yaw": 4.0,
                    "pitch": -20.0,
                    "roll": 0.0,
                    "fov": 70.0,
                },
            }
            for frame in (0, 3)
        ],
    }
    manual = runs_root / "project-1-clip-2/clip-2/01_keyframes/camera_track_manual.json"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(json.dumps(track), encoding="utf-8")
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(manual)},
        },
    )
    assert saved.status == 200

    payload = {
        "direction": "down",
        "expected_revision": repositories.clips.load("project-1").revision,
        "expected_jobs_revision": repositories.jobs.load("project-1").revision,
        "return_to": "/apps/project_workspace/?projectId=project-1",
    }
    preparing = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/locate-adjacent",
        json_body=payload,
    )

    assert preparing.status == 202
    assert preparing.body["state"] == "preparing_clip"
    assert preparing.body["target_clip_id"] == "clip-3"
    job = api.service.queue.get(preparing.body["job_id"])
    assert job.job_type == "clip_export"
    seed_root = api.service.projects_root / "project-1/workbench_seeds/clip-3"
    assert len(list(seed_root.glob("*/workbench_seed_manifest.json"))) == 0

    payload["expected_revision"] = repositories.clips.load("project-1").revision
    payload["expected_jobs_revision"] = repositories.jobs.load("project-1").revision
    repeated = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-2/locate-adjacent",
        json_body=payload,
    )
    assert repeated.status == 202
    assert repeated.body["job_id"] == preparing.body["job_id"]
    assert len(list(seed_root.glob("*/workbench_seed_manifest.json"))) == 0


def test_adjacent_location_rejects_recoverable_saved_target_without_reference(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, _job = _project_api_with_workbench(tmp_path)
    _add_same_scene_adjacent_clips(api, repositories, tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-3/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    manual = runs_root / "project-1-clip-3/clip-3/01_keyframes/camera_track_manual.json"
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(json.dumps(_manual_track()), encoding="utf-8")
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(manual)},
        },
    )
    assert saved.status == 200
    clips = repositories.clips.load("project-1")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(
                replace(
                    clip,
                    references=tuple(
                        reference
                        for reference in clip.references
                        if reference.key != "workbench:clip-3"
                    ),
                )
                if clip.clip_id == "clip-3"
                else clip
                for clip in value.clips
            ),
        ),
    )
    current = repositories.clips.load("project-1")
    source = next(clip for clip in current.clips if clip.clip_id == "clip-2")
    target = next(clip for clip in current.clips if clip.clip_id == "clip-3")

    assert api.workbench._saved_resume_baseline(
        api.workbench.resolve_context("project-1", "clip-3")
    ) is not None
    assert not api.workbench._target_accepts_seed(
        "project-1", source, target, "down"
    )


def test_snapshot_exposes_server_derived_workbench_capability_and_state(
    tmp_path: Path,
) -> None:
    api, _repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")

    clip = snapshot.body["clips"][0]
    assert clip["capabilities"]["can_open_workbench"] is True
    assert clip["workbench"]["state"] == "ready"
    assert clip["workbench"]["workbench_output_revision"] is None


def test_snapshot_keeps_completed_trajectory_as_primary_when_old_render_is_superseded(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, trajectory = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    superseded_render = {
        **trajectory.to_dict(),
        "job_id": "render-old",
        "job_type": "clip_render",
        "status": "superseded",
        "stage": "superseded",
        "output_revision": None,
        "output_fingerprint": None,
        "output_validated": False,
        "validated_input_fingerprint": None,
        "published_outputs": {},
        "validation_proof": None,
    }
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(
            value,
            jobs=(*value.jobs, superseded_render),
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")

    clip = snapshot.body["clips"][0]
    assert clip["status"] == "success"
    assert clip["job_id"] == trajectory.job_id
    assert clip["capabilities"]["can_retry"] is False
    assert clip["render"]["status"] == "superseded"


def test_workbench_uses_project_active_cad_after_global_replacement(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    new_dataset = tmp_path / "cad-dataset-v2"
    new_dataset.mkdir()
    new_design = new_dataset / "design.json"
    new_design.write_text('{"revision": 2}', encoding="utf-8")
    project = repositories.project.load("project-1")
    repositories.project.update(
        "project-1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "cad": {
                    "revision": "cad:v2",
                    "dataset_id": "cad-v2",
                    "dataset_path": str(new_dataset),
                },
            },
        ),
    )
    clip = repositories.clips.load("project-1").clips[0]

    resolved = api.workbench._cad_design_for_context("project-1", clip)

    assert resolved == new_design


def test_batch_completed_trajectory_is_materialized_when_workbench_opens(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, job = _project_api_with_workbench(
        tmp_path, workflow="pure_rotation"
    )
    source_run = Path(job.attempts[-1].directory) / "project-1/clip-1"
    trajectory = source_run / "02_pure_rotation/camera_rotation_raw.json"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_text(
        json.dumps({"trajectory_mode": "pure_rotation_only", "poses": [{"frame": 0}]}),
        encoding="utf-8",
    )
    completed = replace(
        job,
        published_outputs={"trajectory": str(trajectory)},
        output_fingerprint=sha256(trajectory.read_bytes()).hexdigest(),
    )
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=(completed.to_dict(),)),
    )

    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )

    assert opened.status == 201
    assert opened.body["launch_mode"] == "trajectory_ready"
    assert parse_qs(urlsplit(opened.body["workbench_url"]).query)["workflowStage"] == [
        "keyframes"
    ]
    published = (
        runs_root
        / "project-1-clip-1/clip-1/02_pure_rotation/camera_rotation_raw.json"
    )
    assert json.loads(published.read_text(encoding="utf-8"))["poses"]


def test_ready_clip_can_open_workbench_before_trajectory_is_solved(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    clip = snapshot.body["clips"][0]
    assert clip["capabilities"]["can_open_workbench"] is True
    assert clip["workbench"]["state"] == "ready"

    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )

    assert opened.status == 201
    assert opened.body["launch_mode"] == "workflow_start"
    assert "workflowStage=sfm" in opened.body["workbench_url"]
    assert opened.body["trajectory_job_id"] == ""
    query = parse_qs(urlsplit(opened.body["workbench_url"]).query)
    assert query["dataset"] == ["project-1-clip-1"]
    assert slugify_dataset_name(query["dataset"][0]) == query["dataset"][0]
    assert query["projectId"] == ["project-1"]
    assert query["cadScale"] == ["0.06"]
    assert query["originXY"] == ["0,0"]
    assert query["video"] == ["/data/project-1-clip-1/video/project-1-clip-1.mp4"]
    assert query["cad"] == ["/data/project-1-clip-1/cad/design.json"]
    assert (
        tmp_path / "data/project-1-clip-1/video/project-1-clip-1.mp4"
    ).read_bytes() == b"physical-clip"
    assert json.loads(
        (tmp_path / "data/project-1-clip-1/cad/design.json").read_text(encoding="utf-8")
    )["entities"] == [{"type": "line"}]
    bridge = json.loads(
        (tmp_path / "data/project-1-clip-1/dataset_manifest.json").read_text(encoding="utf-8")
    )
    assert bridge["workflow"]["trajectory_mode"] == "sfm_only"


def test_workflow_start_session_is_not_allowed_to_save_before_trajectory(
    session_system,
) -> None:
    coordinator, _store, _clock, current, _validated = session_system
    current[0] = _context(
        trajectory_job_id="",
        trajectory_output_revision="",
        trajectory_output_fingerprint="",
        save_permissions=(),
        can_open_workbench=True,
    )

    session = _create(coordinator)

    assert session.trajectory_job_id == ""
    assert session.save_permissions == ()
    with pytest.raises(WorkbenchPermissionDenied, match="save permission"):
        coordinator.save(
            "project-1",
            session.token,
            {"ok": True},
        )


def test_open_workbench_enqueues_on_demand_clip_export_before_creating_session(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    clips = repositories.clips.load("project-1")
    clip = clips.clips[0]
    analysis = dict(clip.analysis)
    analysis.pop("physical_mp4_path")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, clips=(replace(clip, analysis=analysis),)
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    capability = snapshot.body["clips"][0]["capabilities"]
    assert capability["can_open_workbench"] is False
    assert capability["can_prepare_workbench"] is True
    jobs_revision = repositories.jobs.load("project-1").revision

    preparing = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": jobs_revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )

    assert preparing.status == 202
    assert preparing.body["state"] == "preparing_clip"
    assert preparing.body["message"] == "正在按原视频时间范围准备片段视频"
    queued = repositories.jobs.load("project-1")
    assert queued.revision == jobs_revision + 1
    export = next(
        item for item in queued.jobs if item["job_id"] == preparing.body["job_id"]
    )
    assert export["job_type"] == "clip_export"
    assert export["status"] in {"queued", "preparing", "running"}

    running = api.service.queue.claim_next_unstarted()
    assert running is not None
    assert running.job_id == preparing.body["job_id"]
    attempt = running.attempts[-1]
    output_dir = Path(attempt.directory) / "clip_inputs"
    output_dir.mkdir(parents=True)
    (output_dir / "clip-1.mp4").write_bytes(b"exported-58-second-clip")
    (output_dir / "clip_frame_map.json").write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "clip_id": "clip-1",
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = _validate_clip_export_outputs(
        repositories.clips.load("project-1").clips, output_dir
    )
    api.service.finish_job(
        "project-1",
        running.job_id,
        result,
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    ready = api.handle("GET", "/api/projects/project-1/snapshot")
    assert ready.body["clips"][0]["capabilities"]["can_open_workbench"] is True
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    assert opened.body["launch_mode"] == "workflow_start"
    assert (
        tmp_path / "data/project-1-clip-1/video/project-1-clip-1.mp4"
    ).read_bytes() == b"exported-58-second-clip"


def test_open_workbench_retries_failed_on_demand_clip_export(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    clips = repositories.clips.load("project-1")
    clip = clips.clips[0]
    analysis = dict(clip.analysis)
    analysis.pop("physical_mp4_path")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(value, clips=(replace(clip, analysis=analysis),)),
    )
    endpoint = "/api/projects/project-1/clips/clip-1/workbench-sessions"
    first = api.handle(
        "POST",
        endpoint,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    running = api.service.queue.claim_next_unstarted()
    assert running is not None and running.job_id == first.body["job_id"]
    lease = running.attempts[-1]
    api.service.fail_job(
        "project-1",
        running.job_id,
        "simulated Windows progress sidecar sharing conflict",
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    api.service.queue.release_execution_claim(
        running.job_id,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    retried = api.handle(
        "POST",
        endpoint,
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )

    assert retried.status == 202
    assert retried.body["job_id"] == first.body["job_id"]
    retry_job = api.service.queue.get(first.body["job_id"])
    assert retry_job.status in {"queued", "preparing", "running"}
    assert [attempt.number for attempt in retry_job.attempts] == [1, 2]


def test_user_started_trajectory_is_attached_to_open_workbench_session(
    tmp_path: Path,
) -> None:
    api, repositories, runs_root, job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    assert opened.body["launch_mode"] == "workflow_start"
    assert opened.body["save_permissions"] == []

    source_run = Path(job.attempts[-1].directory) / "project-1/clip-1"
    trajectory = source_run / "02_sfm/camera_trajectory.json"
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    trajectory.write_text(
        json.dumps({"poses": [{"frame_index": 0}]}), encoding="utf-8"
    )
    completed = replace(
        job,
        status="success",
        stage="success",
        output_revision="trajectory-output-user-started",
        output_fingerprint=sha256(trajectory.read_bytes()).hexdigest(),
        output_validated=True,
        validated_input_fingerprint=job.input_fingerprint,
        published_outputs={"trajectory": str(trajectory)},
    )
    current_jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=current_jobs.revision,
        mutate=lambda value: replace(value, jobs=(completed.to_dict(),)),
    )

    attached = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/trajectory-ready",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
        },
    )

    assert attached.status == 200
    assert attached.body["launch_mode"] == "trajectory_ready"
    assert attached.body["trajectory_job_id"] == job.job_id
    assert attached.body["save_permissions"] == ["save"]
    attached_session = api.workbench.inspect("project-1", opened.body["token"])
    attached_query = parse_qs(
        urlsplit(api.workbench.workbench_url(attached_session)).query
    )
    assert attached_query["workflowStage"] == ["keyframes"]
    published = (
        runs_root
        / "project-1-clip-1/clip-1/02_sfm/camera_trajectory.json"
    )
    assert json.loads(published.read_text(encoding="utf-8"))["poses"]
    inspected = api.handle(
        "GET",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}",
    )
    assert inspected.status == 200
    assert inspected.body["trajectory_job_id"] == job.job_id
    manual = (
        runs_root
        / "project-1-clip-1/clip-1/01_keyframes/camera_track_manual.json"
    )
    manual.parent.mkdir(parents=True, exist_ok=True)
    manual.write_text(json.dumps(_manual_track()), encoding="utf-8")
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(manual)},
        },
    )
    assert saved.status == 200
    assert saved.body["state"] == "saved"


def test_snapshot_projects_active_clip_export_as_batch_trajectory_progress(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(
        tmp_path, workflow="pure_rotation"
    )
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    clips = repositories.clips.load("project-1")
    clip = clips.clips[0]
    analysis = dict(clip.analysis)
    analysis.pop("physical_mp4_path")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, clips=(replace(clip, analysis=analysis),)
        ),
    )

    enqueued = api.handle(
        "POST",
        "/api/projects/project-1/trajectory-jobs",
        json_body={
            "expected_revision": repositories.jobs.load("project-1").revision,
            "clip_ids": ["clip-1"],
            "confirmed_clip_ids": [],
            "enqueue": True,
        },
    )

    assert enqueued.status == 202
    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    payload = snapshot.body["clips"][0]
    trajectory = next(
        item
        for item in repositories.jobs.load("project-1").jobs
        if item["job_type"] == "trajectory"
    )
    assert trajectory["status"] == "queued"
    assert payload["job_id"] == trajectory["job_id"]
    assert payload["status"] == "running"
    assert payload["stage"] == "running"
    assert payload["progress"]["fraction"] == 0.0


def test_snapshot_combines_export_and_pure_rotation_into_monotonic_progress(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(
        tmp_path, workflow="pure_rotation"
    )
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    clips = repositories.clips.load("project-1")
    clip = clips.clips[0]
    analysis = dict(clip.analysis)
    analysis.pop("physical_mp4_path")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, clips=(replace(clip, analysis=analysis),)
        ),
    )
    enqueued = api.handle(
        "POST",
        "/api/projects/project-1/trajectory-jobs",
        json_body={
            "expected_revision": repositories.jobs.load("project-1").revision,
            "clip_ids": ["clip-1"],
            "confirmed_clip_ids": [],
            "enqueue": True,
        },
    )
    assert enqueued.status == 202
    manifest = repositories.jobs.load("project-1")
    active_export = next(
        item for item in manifest.jobs if item["job_type"] == "clip_export"
    )
    repositories.jobs.update(
        "project-1",
        expected_revision=manifest.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(
                {
                    **item,
                    "status": "validating",
                    "stage": "validating",
                    "progress": {
                        "stage": "validating",
                        "message": "source frames exported; validating",
                        "fraction": 1.0,
                    },
                }
                if item["job_id"] == active_export["job_id"]
                else item
                for item in value.jobs
            ),
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    payload = snapshot.body["clips"][0]

    assert payload["status"] == "validating"
    assert payload["progress"]["stage"] == "validating"
    assert payload["progress"]["fraction"] == pytest.approx(0.1485)

    manifest = repositories.jobs.load("project-1")
    trajectory = next(
        item for item in manifest.jobs if item["job_type"] == "trajectory"
    )
    repositories.jobs.update(
        "project-1",
        expected_revision=manifest.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(
                {
                    **item,
                    "status": "success",
                    "stage": "success",
                    "progress": {
                        "stage": "complete",
                        "message": "clip export completed",
                        "fraction": 1.0,
                    },
                }
                if item["job_id"] == active_export["job_id"]
                else {
                    **item,
                    "status": "running",
                    "stage": "running",
                    "progress": {
                        "stage": "running",
                        "message": "trajectory adapter is running",
                    },
                }
                if item["job_id"] == trajectory["job_id"]
                else item
                for item in value.jobs
            ),
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    payload = snapshot.body["clips"][0]

    assert payload["status"] == "running"
    assert payload["progress"]["stage"] == "running"
    assert payload["progress"]["message"] == "trajectory adapter is running"
    assert payload["progress"]["fraction"] == 0.15

    manifest = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=manifest.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(
                {
                    **item,
                    "progress": {
                        "stage": "pure_rotation_frames",
                        "message": "正在反算 100 / 200 帧",
                        "fraction": 0.465,
                    },
                }
                if item["job_id"] == trajectory["job_id"]
                else item
                for item in value.jobs
            ),
        ),
    )
    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    payload = snapshot.body["clips"][0]

    assert payload["progress"]["fraction"] == pytest.approx(0.54525)


def test_snapshot_exposes_user_facing_candidate_analysis_clip_preview(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    artifact = tmp_path / "candidate-analysis"
    output = artifact / "02_video_analysis"
    output.mkdir(parents=True)
    (output / "clip_manifest.json").write_text(
        json.dumps(
            {
                "analysis_revision": "analysis-2",
                "clips": [
                    {
                        "clip_id": "clip-candidate-1",
                        "analysis_revision": "analysis-2",
                        "scene_index": 2,
                        "segment_index": 1,
                        "source_start_pts": 250,
                        "source_end_pts_exclusive": 1500,
                        "source_time_base": {
                            "numerator": 1,
                            "denominator": 25,
                        },
                        "interval_semantics": "half_open",
                        "detected_motion_mode": "rotation_dominant",
                        "confidence": 0.88,
                        "recommended_workflow": "pure_rotation",
                        "needs_review": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project = repositories.project.load("project-1")
    input_snapshot = {
        "request_key": "analysis-request-2",
        "video": {"path": str(tmp_path / "source.mp4"), "sha256": "v" * 64},
        "cad": None,
        "srt": None,
        "analysis_artifact": {
            "path": str(artifact),
            "artifact_id": "candidate-analysis",
        },
    }
    repositories.project.update(
        "project-1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            register_analysis_revision(
                value, "analysis-2", operation_id="candidate-operation"
            ),
            source_assets={
                **value.source_assets,
                "_analysis_revisions": {
                    "analysis-2": {
                        "input_snapshot": input_snapshot,
                        "analysis_artifact_id": "candidate-analysis",
                        "analysis_artifact_path": str(artifact),
                    }
                },
            },
            project_state="analysis_candidate_ready",
        ),
    )

    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")

    assert snapshot.status == 200
    assert snapshot.body["candidate_analysis_preview"] == {
        "clip_count": 1,
        "clips": [
            {
                "display_name": "场景 02 · 第 1 段",
                "time_range": "00:10 – 01:00",
                "duration": "00:50",
                "detected_motion_mode": "rotation_dominant",
                "confidence": 0.88,
                "recommended_workflow": "pure_rotation",
                "needs_review": False,
            }
        ],
    }


def test_workbench_heartbeat_extends_editing_session_lease(
    tmp_path: Path,
) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    jobs = repositories.jobs.load("project-1")
    repositories.jobs.update(
        "project-1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(value, jobs=()),
    )
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    original_expiry = opened.body["expires_at"]
    clock = api.workbench.coordinator.now
    clock.value += timedelta(minutes=29)

    renewed = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/heartbeat",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
        },
    )

    assert renewed.status == 200
    assert renewed.body["state"] == "editing"
    assert renewed.body["expires_at"] > original_expiry
    reference = repositories.clips.load("project-1").clips[0].references[-1]
    assert reference.value["expires_at"] == renewed.body["expires_at"]
    clock.value += timedelta(minutes=2)
    inspected = api.handle(
        "GET",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}",
    )
    assert inspected.status == 200
    assert inspected.body["state"] == "editing"


def test_workbench_heartbeat_does_not_revive_expired_session(tmp_path: Path) -> None:
    api, repositories, _runs_root, _job = _project_api_with_workbench(tmp_path)
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "expected_jobs_revision": repositories.jobs.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    clock = api.workbench.coordinator.now
    clock.value += timedelta(minutes=31)

    renewed = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/heartbeat",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
        },
    )

    assert renewed.status == 409
    assert renewed.body["error"] == "stale_workbench_session"


def test_snapshot_never_binds_historical_success_from_an_old_clip_input(
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
    assert snapshot.body["clips"][0]["capabilities"]["can_open_workbench"] is True
    opened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert opened.status == 201
    assert opened.body["launch_mode"] == "workflow_start"
    assert opened.body["trajectory_job_id"] == ""


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
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
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
    bootstrap = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{token}"
    )
    assert bootstrap.status == 409
    assert bootstrap.body["error"] == "stale_workbench_session"


def test_workbench_inspect_rejects_workflow_change_and_stale_reference(
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
    clips = repositories.clips.load("project-1")
    api.service.update_clip_workflow(
        "project-1",
        "clip-1",
        expected_revision=clips.revision,
        workflow_override="pure_rotation",
    )

    bootstrap = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{token}"
    )

    assert bootstrap.status == 409
    assert bootstrap.body["error"] == "stale_workbench_session"


def test_workbench_inspect_rejects_missing_clip_reference(tmp_path: Path) -> None:
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
    clips = repositories.clips.load("project-1")
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, clips=(replace(value.clips[0], references=()),)
        ),
    )

    bootstrap = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{token}"
    )

    assert bootstrap.status == 409
    assert bootstrap.body["error"] == "stale_workbench_session"


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
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
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

    manifest_path = (
        tmp_path
        / "projects/project-1/workbench_outputs/workbench-output-api-1/workbench_output_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = manifest["artifacts"]["camera_track"]
    assert not Path(artifact["path"]).is_absolute()
    immutable = manifest_path.parent / artifact["path"]
    before = immutable.read_bytes()
    assert sha256(before).hexdigest() == artifact["sha256"]
    output.write_text(json.dumps({"overwritten": True}), encoding="utf-8")
    assert immutable.read_bytes() == before


@pytest.mark.parametrize(
    "track",
    [
        {"fps": 25, "keyframes": ["bad"]},
        {"fps": 25, "keyframes": [{"frame": 0}]},
        {"fps": 25, "keyframes": [{"frame": True, "camera": {}}]},
        {"fps": float("nan"), "keyframes": []},
        {
            "fps": 25,
            "keyframes": [{
                "frame": 0,
                "camera": {
                    "x": float("inf"), "y": 0, "z": 0,
                    "yaw": 0, "pitch": 0, "roll": 0, "fov": 70,
                },
            }],
        },
    ],
)
def test_manual_track_authoritative_validation_rejects_malformed_values(
    tmp_path: Path, track: dict[str, object]
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
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(track), encoding="utf-8")
    rejected = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )
    assert rejected.status == 400
    assert "camera track" in rejected.body["error"]


def test_manual_track_publication_uses_the_exact_validated_byte_snapshot(
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
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
    validated_bytes = output.read_bytes()
    changed_track = _manual_track()
    changed_track["keyframes"][0]["camera"]["x"] = 999.0
    original_validator = api.workbench.coordinator.validate_output

    def validate_then_replace(session, receipt):
        validated = original_validator(session, receipt)
        output.write_text(json.dumps(changed_track), encoding="utf-8")
        return validated

    api.workbench.coordinator.validate_output = validate_then_replace
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )

    assert saved.status == 200
    immutable = (
        tmp_path
        / "projects/project-1/workbench_outputs/workbench-output-api-1"
        / "artifacts/camera_track_manual.json"
    )
    assert immutable.read_bytes() == validated_bytes


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
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
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
    assert (
        reference.value["trajectory_output_fingerprint"]
        == session.trajectory_output_fingerprint
    )
    with pytest.raises(ReplayedWorkbenchSave):
        api.workbench.save(
            "project-1",
            token,
            {"ok": True, "path": str(output)},
            expected_clips_revision=repositories.clips.load("project-1").revision,
        )


@pytest.mark.parametrize("action", ["save", "close"])
def test_stale_saved_session_never_repairs_after_current_input_changes(
    tmp_path: Path, action: str
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
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
    assert api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    ).status == 200
    clips = repositories.clips.load("project-1")
    old_reference = clips.clips[0].references[-1]
    stale_reference = replace(
        old_reference, value={**old_reference.value, "status": "stale"}
    )
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            clips=(replace(
                value.clips[0],
                manual_definition={"changed": True},
                references=(*value.clips[0].references[:-1], stale_reference),
            ),),
        ),
    )
    payload: dict[str, object] = {
        "expected_revision": repositories.clips.load("project-1").revision
    }
    if action == "save":
        payload["existing_save"] = {"ok": True, "path": str(output)}

    rejected = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{token}/{action}",
        json_body=payload,
    )

    assert rejected.status == 409
    assert rejected.body["error"] == "stale_workbench_session"
    current_reference = repositories.clips.load("project-1").clips[0].references[-1]
    assert current_reference.value["status"] == "stale"


def test_stale_saved_reference_allows_new_session_and_old_token_cannot_overwrite_it(
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
    old_token = opened.body["token"]
    output = runs_root / "project-1/clip-1/01_keyframes/camera_track_manual.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
    assert api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{old_token}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    ).status == 200
    clips = repositories.clips.load("project-1")
    old_reference = clips.clips[0].references[-1]
    repositories.clips.update(
        "project-1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value,
            clips=(replace(
                value.clips[0],
                references=(*value.clips[0].references[:-1], replace(
                    old_reference,
                    value={**old_reference.value, "status": "stale"},
                )),
            ),),
        ),
    )
    replacement = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )
    assert replacement.status == 201
    replacement_reference = repositories.clips.load("project-1").clips[0].references[-1]
    old_bootstrap = api.handle(
        "GET", f"/api/projects/project-1/workbench-sessions/{old_token}"
    )
    assert old_bootstrap.status == 409
    assert old_bootstrap.body["error"] == "stale_workbench_session"

    rejected = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{old_token}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "path": str(output)},
        },
    )

    assert rejected.status == 409
    assert repositories.clips.load("project-1").clips[0].references[-1] == replacement_reference


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
    (corrected.parent / "correction_lineage.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_sha256": sha256(base.read_bytes()).hexdigest(),
                "corrected_sha256": sha256(corrected.read_bytes()).hexdigest(),
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
    assert manifest["source_artifact_name"] == "camera_track_corrected.json"
    assert manifest["source_output_revision"].startswith("pure-track:")


def test_reopen_restores_saved_pure_rotation_progress_and_resumes_render(
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
    run = runs_root / "project-1-clip-1/clip-1"
    saved_track = run / "03_pure_rotation_placement/camera_track_cad_base.json"
    saved_track.parent.mkdir(parents=True, exist_ok=True)
    saved_payload = {
        "schema_version": 1,
        "trajectory_mode": "pure_rotation_manual_calibrated",
        "display_fov": 58.0,
        "poses": [
            {
                "decoded_frame_index": 0,
                "pts_time_sec": 0.0,
                "segment_id": 0,
                "rotation_cad_from_camera": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                "camera_center_web": [10, 20, 30],
            }
        ],
    }
    saved_track.write_text(json.dumps(saved_payload), encoding="utf-8")
    saved_bytes = saved_track.read_bytes()
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "kind": "pure_rotation_calibration"},
        },
    )
    assert saved.status == 200

    reopened = api.handle(
        "POST",
        "/api/projects/project-1/clips/clip-1/workbench-sessions",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "return_to": "/apps/project_workspace/?projectId=project-1",
        },
    )

    assert reopened.status == 201
    assert (
        reopened.body["workbench_output_revision"]
        == saved.body["workbench_output_revision"]
    )
    assert parse_qs(urlsplit(reopened.body["workbench_url"]).query)[
        "workflowStage"
    ] == ["render"]
    assert saved_track.read_bytes() == saved_bytes
    placement = json.loads(
        (run / "03_pure_rotation_placement/global_camera_placement.json").read_text(
            encoding="utf-8"
        )
    )
    assert placement["camera_center_web"] == [10.0, 20.0, 30.0]
    reference = repositories.clips.load("project-1").clips[0].references[-1]
    assert reference.value["status"] == "editing"
    assert (
        reference.value["workbench_output_revision"]
        == saved.body["workbench_output_revision"]
    )

    closed = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{reopened.body['token']}/close",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
        },
    )
    assert closed.status == 200
    assert closed.body["state"] == "saved"
    snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
    workbench = snapshot.body["clips"][0]["workbench"]
    assert workbench["state"] == "saved"
    assert (
        workbench["workbench_output_revision"]
        == saved.body["workbench_output_revision"]
    )


def test_stale_pure_rotation_corrected_lineage_falls_back_to_current_base(
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
    payload = {
        "trajectory_mode": "pure_rotation_manual_calibrated",
        "poses": [{
            "decoded_frame_index": 0,
            "rotation_cad_from_camera": [[1,0,0],[0,1,0],[0,0,1]],
            "camera_center_web": [1,2,3],
        }],
    }
    base.parent.mkdir(parents=True)
    corrected.parent.mkdir(parents=True)
    base.write_text(json.dumps(payload), encoding="utf-8")
    corrected.write_text(json.dumps(payload), encoding="utf-8")
    (corrected.parent / "correction_lineage.json").write_text(
        json.dumps({
            "schema_version": 1,
            "base_sha256": "0" * 64,
            "corrected_sha256": sha256(corrected.read_bytes()).hexdigest(),
        }),
        encoding="utf-8",
    )
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "kind": "pure_rotation_calibration"},
        },
    )
    assert saved.status == 200
    manifest = json.loads((
        tmp_path / "projects/project-1/workbench_outputs"
        / saved.body["workbench_output_revision"] / "workbench_output_manifest.json"
    ).read_text(encoding="utf-8"))
    assert manifest["source_artifact_name"] == "camera_track_cad_base.json"


def test_pure_rotation_publication_uses_the_exact_validated_byte_snapshot(
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
    base = (
        runs_root
        / "project-1/clip-1/03_pure_rotation_placement/camera_track_cad_base.json"
    )
    base.parent.mkdir(parents=True)
    payload = {
        "trajectory_mode": "pure_rotation_manual_calibrated",
        "poses": [{
            "decoded_frame_index": 0,
            "rotation_cad_from_camera": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "camera_center_web": [1, 2, 3],
        }],
    }
    base.write_text(json.dumps(payload), encoding="utf-8")
    validated_bytes = base.read_bytes()
    changed = {**payload, "poses": [{**payload["poses"][0], "decoded_frame_index": 9}]}
    original_validator = api.workbench.coordinator.validate_output

    def validate_then_replace(session, receipt):
        validated = original_validator(session, receipt)
        base.write_text(json.dumps(changed), encoding="utf-8")
        return validated

    api.workbench.coordinator.validate_output = validate_then_replace
    saved = api.handle(
        "POST",
        f"/api/projects/project-1/workbench-sessions/{opened.body['token']}/save",
        json_body={
            "expected_revision": repositories.clips.load("project-1").revision,
            "existing_save": {"ok": True, "kind": "pure_rotation_calibration"},
        },
    )

    assert saved.status == 200
    immutable = (
        tmp_path
        / "projects/project-1/workbench_outputs/workbench-output-api-1"
        / "artifacts/camera_track_cad_base.json"
    )
    assert immutable.read_bytes() == validated_bytes


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
    output.write_text(json.dumps(_manual_track()), encoding="utf-8")
    editing_snapshot = api.handle("GET", "/api/projects/project-1/snapshot")
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

    pending_snapshot = api.handle(
        "GET",
        "/api/projects/project-1/snapshot",
        headers={"If-None-Match": editing_snapshot.headers["ETag"]},
    )
    assert pending_snapshot.status == 200
    pending_clip = pending_snapshot.body["clips"][0]
    assert pending_clip["workbench"]["state"] == "pending_save"
    assert pending_clip["capabilities"]["can_open_workbench"] is False

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
