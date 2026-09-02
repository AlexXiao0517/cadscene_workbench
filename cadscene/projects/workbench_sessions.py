from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from threading import Lock, RLock
from typing import Callable, Mapping, Protocol
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from .identifiers import validate_project_id
from .json_repositories import ProjectRepositories
from .models import ClipDefinition, StateReference
from .queue import QueueJob
from .repositories import RevisionConflict
from .workbench_resume import AtomicWorkbenchResumeStore, WorkbenchResumeState
from .scene_bridge_runner import validate_scene_bridge_candidate
from .service import ProjectService
from cadscene.alignment.keyframes import confirmed_keyframes
from cadscene.pure_rotation.artifact_lock import pure_rotation_run_lock
from cadscene.workflow.data_import import slugify_dataset_name


SCHEMA_VERSION = "1.0"
_LOCKS_GUARD = Lock()
_RECORD_LOCKS: dict[str, RLock] = {}


class WorkbenchSessionError(RuntimeError):
    pass


class InvalidWorkbenchReturnPath(WorkbenchSessionError):
    pass


class StaleWorkbenchSession(WorkbenchSessionError):
    pass


class WorkbenchPermissionDenied(WorkbenchSessionError):
    pass


class ReplayedWorkbenchSave(WorkbenchSessionError):
    pass


class InvalidWorkbenchOutput(WorkbenchSessionError):
    pass


@dataclass(frozen=True)
class WorkbenchContext:
    project_id: str
    clip_id: str
    workflow: str
    project_input_revision: str
    clip_input_revision: str
    input_fingerprint: str
    trajectory_job_id: str
    trajectory_run_id: str
    trajectory_output_revision: str
    trajectory_output_fingerprint: str
    save_permissions: tuple[str, ...]
    can_open_workbench: bool


@dataclass(frozen=True)
class WorkbenchSession:
    schema_version: str
    revision: int
    updated_at: str
    operation_id: str
    token: str
    project_id: str
    clip_id: str
    workflow: str
    project_input_revision: str
    clip_input_revision: str
    input_fingerprint: str
    trajectory_job_id: str
    trajectory_run_id: str
    trajectory_output_revision: str
    trajectory_output_fingerprint: str
    launch_mode: str
    save_permissions: tuple[str, ...]
    created_at: str
    expires_at: str
    return_to: str
    state: str
    workbench_output_revision: str | None = None
    workbench_output_fingerprint: str | None = None
    workbench_output_operation_id: str | None = None
    pending_output_revision: str | None = None
    pending_source_output_revision: str | None = None
    pending_source_output_fingerprint: str | None = None
    pending_receipt_fingerprint: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "operation_id": self.operation_id,
            "token": self.token,
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "workflow": self.workflow,
            "project_input_revision": self.project_input_revision,
            "clip_input_revision": self.clip_input_revision,
            "input_fingerprint": self.input_fingerprint,
            "trajectory_job_id": self.trajectory_job_id,
            "trajectory_run_id": self.trajectory_run_id,
            "trajectory_output_revision": self.trajectory_output_revision,
            "trajectory_output_fingerprint": self.trajectory_output_fingerprint,
            "launch_mode": self.launch_mode,
            "save_permissions": list(self.save_permissions),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "return_to": self.return_to,
            "state": self.state,
            "workbench_output_revision": self.workbench_output_revision,
            "workbench_output_fingerprint": self.workbench_output_fingerprint,
            "workbench_output_operation_id": self.workbench_output_operation_id,
            "pending_output_revision": self.pending_output_revision,
            "pending_source_output_revision": self.pending_source_output_revision,
            "pending_source_output_fingerprint": self.pending_source_output_fingerprint,
            "pending_receipt_fingerprint": self.pending_receipt_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkbenchSession:
        return cls(
            schema_version=str(value["schema_version"]),
            revision=int(value["revision"]),
            updated_at=str(value["updated_at"]),
            operation_id=str(value["operation_id"]),
            token=str(value["token"]),
            project_id=str(value["project_id"]),
            clip_id=str(value["clip_id"]),
            workflow=str(value["workflow"]),
            project_input_revision=str(value["project_input_revision"]),
            clip_input_revision=str(value["clip_input_revision"]),
            input_fingerprint=str(value["input_fingerprint"]),
            trajectory_job_id=str(value["trajectory_job_id"]),
            trajectory_run_id=str(value["trajectory_run_id"]),
            trajectory_output_revision=str(value["trajectory_output_revision"]),
            trajectory_output_fingerprint=str(
                value["trajectory_output_fingerprint"]
            ),
            launch_mode=str(
                value.get("launch_mode")
                or (
                    "trajectory_ready"
                    if value.get("trajectory_job_id")
                    else "workflow_start"
                )
            ),
            save_permissions=tuple(str(item) for item in value["save_permissions"]),
            created_at=str(value["created_at"]),
            expires_at=str(value["expires_at"]),
            return_to=str(value["return_to"]),
            state=str(value["state"]),
            workbench_output_revision=_optional_string(
                value.get("workbench_output_revision")
            ),
            workbench_output_fingerprint=_optional_string(
                value.get("workbench_output_fingerprint")
            ),
            workbench_output_operation_id=_optional_string(
                value.get("workbench_output_operation_id")
                or (
                    value.get("operation_id")
                    if value.get("workbench_output_revision")
                    and value.get("workbench_output_fingerprint")
                    else None
                )
            ),
            pending_output_revision=_optional_string(
                value.get("pending_output_revision")
            ),
            pending_source_output_revision=_optional_string(
                value.get("pending_source_output_revision")
            ),
            pending_source_output_fingerprint=_optional_string(
                value.get("pending_source_output_fingerprint")
            ),
            pending_receipt_fingerprint=_optional_string(
                value.get("pending_receipt_fingerprint")
            ),
        )


class WorkbenchSessionStore(Protocol):
    def load(self, project_id: str, token: str) -> WorkbenchSession: ...

    def create(self, session: WorkbenchSession) -> WorkbenchSession: ...

    def load_by_token_hash(
        self, project_id: str, token_hash: str
    ) -> WorkbenchSession: ...

    def list_for_project(self, project_id: str) -> tuple[WorkbenchSession, ...]: ...

    def update(
        self,
        project_id: str,
        token: str,
        *,
        expected_revision: int,
        mutate: Callable[[WorkbenchSession], WorkbenchSession],
    ) -> WorkbenchSession: ...


class AtomicWorkbenchSessionStore:
    """Project-local, per-token atomic credential records, not a state manifest."""

    def __init__(
        self, root: Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self.root = root
        self.now = now or (lambda: datetime.now(timezone.utc))

    def path_for(self, project_id: str, token: str) -> Path:
        project_id = validate_project_id(project_id)
        digest = sha256(token.encode("utf-8")).hexdigest()
        return self.root / project_id / "workbench_sessions" / f"{digest}.json"

    def path_for_token_hash(self, project_id: str, token_hash: str) -> Path:
        project_id = validate_project_id(project_id)
        if (
            len(token_hash) != 64
            or any(character not in "0123456789abcdef" for character in token_hash)
        ):
            raise ValueError("invalid workbench session token hash")
        return self.root / project_id / "workbench_sessions" / f"{token_hash}.json"

    def lock_for(self, project_id: str, token: str) -> RLock:
        path = self.path_for(project_id, token).resolve(strict=False)
        key = os.path.normcase(str(path))
        with _LOCKS_GUARD:
            return _RECORD_LOCKS.setdefault(key, RLock())

    def load(self, project_id: str, token: str) -> WorkbenchSession:
        with self.lock_for(project_id, token):
            path = self.path_for(project_id, token)
            with path.open("r", encoding="utf-8") as stream:
                session = WorkbenchSession.from_dict(json.load(stream))
            self._validate_identity(project_id, token, session)
            return session

    def load_by_token_hash(
        self, project_id: str, token_hash: str
    ) -> WorkbenchSession:
        path = self.path_for_token_hash(project_id, token_hash)
        with _path_record_lock(path):
            with path.open("r", encoding="utf-8") as stream:
                session = WorkbenchSession.from_dict(json.load(stream))
            if sha256(session.token.encode("utf-8")).hexdigest() != token_hash:
                raise ValueError("workbench session token hash mismatch")
            self._validate_identity(project_id, session.token, session)
            return session

    def list_for_project(self, project_id: str) -> tuple[WorkbenchSession, ...]:
        directory = self.root / validate_project_id(project_id) / "workbench_sessions"
        if not directory.is_dir():
            return ()
        sessions: list[WorkbenchSession] = []
        for path in directory.glob("*.json"):
            try:
                with _path_record_lock(path):
                    with path.open("r", encoding="utf-8") as stream:
                        session = WorkbenchSession.from_dict(json.load(stream))
                    self._validate_identity(project_id, session.token, session)
                    if self.path_for(project_id, session.token) != path:
                        continue
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            sessions.append(session)
        return tuple(sorted(sessions, key=lambda item: item.updated_at))

    def create(self, session: WorkbenchSession) -> WorkbenchSession:
        with self.lock_for(session.project_id, session.token):
            path = self.path_for(session.project_id, session.token)
            if path.exists():
                raise FileExistsError("workbench session token already exists")
            if session.revision != 0:
                raise ValueError("new workbench session revision must be zero")
            self._validate_identity(session.project_id, session.token, session)
            self._atomic_write(path, session)
            return self.load(session.project_id, session.token)

    def update(
        self,
        project_id: str,
        token: str,
        *,
        expected_revision: int,
        mutate: Callable[[WorkbenchSession], WorkbenchSession],
    ) -> WorkbenchSession:
        with self.lock_for(project_id, token):
            current = self.load(project_id, token)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            candidate = mutate(current)
            if candidate.revision != current.revision:
                raise ValueError("session mutator must not manage revision")
            advanced = replace(
                candidate,
                revision=current.revision + 1,
                updated_at=_timestamp(self.now()),
            )
            self._validate_identity(project_id, token, advanced)
            self._atomic_write(self.path_for(project_id, token), advanced)
            return self.load(project_id, token)

    @staticmethod
    def _validate_identity(
        project_id: str, token: str, session: WorkbenchSession
    ) -> None:
        if session.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported workbench session schema")
        if session.project_id != project_id or session.token != token:
            raise ValueError("workbench session identity mismatch")
        if session.revision < 0 or not session.updated_at or not session.operation_id:
            raise ValueError("invalid workbench session record header")

    @staticmethod
    def _atomic_write(path: Path, session: WorkbenchSession) -> None:
        serialized = (
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class WorkbenchSessionCoordinator:
    def __init__(
        self,
        *,
        store: WorkbenchSessionStore,
        outputs_root: Path,
        resolve_context: Callable[[str, str], WorkbenchContext],
        validate_output: Callable[
            [WorkbenchSession, Mapping[str, object]], Mapping[str, object]
        ],
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        revision_factory: Callable[[], str] | None = None,
        operation_factory: Callable[[], str] | None = None,
        ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self.store = store
        self.outputs_root = outputs_root
        self.resolve_context = resolve_context
        self.validate_output = validate_output
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self.revision_factory = revision_factory or (
            lambda: f"workbench-{uuid4().hex}"
        )
        self.operation_factory = operation_factory or (lambda: uuid4().hex)
        self.ttl = ttl

    def create(
        self,
        project_id: str,
        clip_id: str,
        *,
        return_to: str,
    ) -> WorkbenchSession:
        context = self.resolve_context(project_id, clip_id)
        _validate_context_identity(context, project_id, clip_id)
        if not context.can_open_workbench:
            raise WorkbenchPermissionDenied("server capability forbids workbench")
        trajectory_ready = bool(
            context.trajectory_output_revision
            and context.trajectory_output_fingerprint
            and context.trajectory_job_id
            and context.trajectory_run_id
        )
        if not trajectory_ready and context.save_permissions:
            raise StaleWorkbenchSession("current trajectory output is unavailable")
        validated_return = validate_return_to(return_to, project_id, clip_id)
        token = self.token_factory()
        if len(token) < 32:
            raise ValueError("workbench session token must contain at least 32 characters")
        created = self.now()
        session = WorkbenchSession(
            schema_version=SCHEMA_VERSION,
            revision=0,
            updated_at=_timestamp(created),
            operation_id=self.operation_factory(),
            token=token,
            project_id=project_id,
            clip_id=clip_id,
            workflow=context.workflow,
            project_input_revision=context.project_input_revision,
            clip_input_revision=context.clip_input_revision,
            input_fingerprint=context.input_fingerprint,
            trajectory_job_id=context.trajectory_job_id,
            trajectory_run_id=context.trajectory_run_id,
            trajectory_output_revision=context.trajectory_output_revision,
            trajectory_output_fingerprint=context.trajectory_output_fingerprint,
            launch_mode=("trajectory_ready" if trajectory_ready else "workflow_start"),
            save_permissions=tuple(context.save_permissions),
            created_at=_timestamp(created),
            expires_at=_timestamp(created + self.ttl),
            return_to=validated_return,
            state="editing",
        )
        return self.store.create(session)

    def inspect(self, project_id: str, token: str) -> WorkbenchSession:
        session = self.store.load(project_id, token)
        if session.state == "editing" and self._expired(session):
            return self.store.update(
                project_id,
                token,
                expected_revision=session.revision,
                mutate=lambda value: replace(
                    value, state="ready", operation_id=self.operation_factory()
                ),
            )
        return session

    def save(
        self,
        project_id: str,
        token: str,
        receipt: Mapping[str, object],
    ) -> WorkbenchSession:
        session = self.store.load(project_id, token)
        if session.state == "saved":
            raise ReplayedWorkbenchSave("workbench session was already saved")
        if session.state not in {"editing", "pending_save"}:
            raise StaleWorkbenchSession("workbench session is not editable")
        if session.state == "editing" and self._expired(session):
            self.inspect(project_id, token)
            raise StaleWorkbenchSession("workbench session expired")
        context = self.resolve_context(project_id, session.clip_id)
        self._validate_binding(session, context)
        if "save" not in session.save_permissions or "save" not in context.save_permissions:
            raise WorkbenchPermissionDenied("workbench session has no save permission")
        if session.state == "pending_save":
            operation_id = session.operation_id
            revision = str(session.pending_output_revision or "")
            source_revision = session.pending_source_output_revision
            source_fingerprint = session.pending_source_output_fingerprint
            receipt_fingerprint = session.pending_receipt_fingerprint
            if (
                not revision
                or not source_revision
                or not source_fingerprint
                or not receipt_fingerprint
            ):
                raise InvalidWorkbenchOutput("pending save metadata is incomplete")
            if _receipt_fingerprint(receipt) != receipt_fingerprint:
                raise InvalidWorkbenchOutput(
                    "validated receipt differs from the pending save"
                )
            target = (
                self.outputs_root
                / validate_project_id(session.project_id)
                / "workbench_outputs"
                / revision
            )
            if target.exists():
                fingerprint = self._validate_existing_output(
                    target,
                    session=session,
                    revision=revision,
                    operation_id=operation_id,
                    source_revision=source_revision,
                    source_fingerprint=source_fingerprint,
                )
                return self._mark_saved(
                    project_id,
                    token,
                    pending=session,
                    revision=revision,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                )
        validated = self.validate_output(session, receipt)
        if not isinstance(validated, Mapping):
            raise InvalidWorkbenchOutput("validator did not return structured output")
        source_revision = validated.get("source_output_revision")
        source_fingerprint = validated.get("source_output_fingerprint")
        if not isinstance(source_revision, str) or not source_revision:
            raise InvalidWorkbenchOutput("validated source output revision is missing")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise InvalidWorkbenchOutput("validated source output fingerprint is missing")
        if session.state == "pending_save":
            if (
                source_revision != session.pending_source_output_revision
                or source_fingerprint != session.pending_source_output_fingerprint
            ):
                raise InvalidWorkbenchOutput(
                    "validated receipt differs from the pending save"
                )
            operation_id = session.operation_id
            revision = str(session.pending_output_revision or "")
            pending = session
        else:
            operation_id = self.operation_factory()
            revision = self.revision_factory()
            pending = self.store.update(
                project_id,
                token,
                expected_revision=session.revision,
                mutate=lambda value: replace(
                    value,
                    state="pending_save",
                    operation_id=operation_id,
                    pending_output_revision=revision,
                    pending_source_output_revision=source_revision,
                    pending_source_output_fingerprint=source_fingerprint,
                    pending_receipt_fingerprint=_receipt_fingerprint(receipt),
                ),
            )
        fingerprint = self._publish_output(
            pending,
            revision=revision,
            operation_id=operation_id,
            validated=validated,
        )
        return self._mark_saved(
            project_id,
            token,
            pending=pending,
            revision=revision,
            operation_id=operation_id,
            fingerprint=fingerprint,
        )

    def _mark_saved(
        self,
        project_id: str,
        token: str,
        *,
        pending: WorkbenchSession,
        revision: str,
        operation_id: str,
        fingerprint: str,
    ) -> WorkbenchSession:
        return self.store.update(
            project_id,
            token,
            expected_revision=pending.revision,
            mutate=lambda value: replace(
                value,
                state="saved",
                operation_id=operation_id,
                workbench_output_revision=revision,
                workbench_output_fingerprint=fingerprint,
                workbench_output_operation_id=operation_id,
                pending_output_revision=None,
                pending_source_output_revision=None,
                pending_source_output_fingerprint=None,
                pending_receipt_fingerprint=None,
            ),
        )

    def abandon(self, project_id: str, token: str) -> WorkbenchSession:
        session = self.store.load(project_id, token)
        if session.state in {"saved", "pending_save"}:
            return session
        if session.state == "ready":
            return session
        return self.store.update(
            project_id,
            token,
            expected_revision=session.revision,
            mutate=lambda value: replace(
                value, state="ready", operation_id=self.operation_factory()
            ),
        )

    def _expired(self, session: WorkbenchSession) -> bool:
        return self.now() >= _parse_timestamp(session.expires_at)

    @staticmethod
    def _validate_binding(
        session: WorkbenchSession, context: WorkbenchContext
    ) -> None:
        checks = (
            ("project input", session.project_input_revision, context.project_input_revision),
            ("clip input", session.clip_input_revision, context.clip_input_revision),
            ("fingerprint", session.input_fingerprint, context.input_fingerprint),
            ("workflow", session.workflow, context.workflow),
            ("trajectory run", session.trajectory_job_id, context.trajectory_job_id),
            ("trajectory run", session.trajectory_run_id, context.trajectory_run_id),
            (
                "trajectory output",
                session.trajectory_output_revision,
                context.trajectory_output_revision,
            ),
            (
                "trajectory output",
                session.trajectory_output_fingerprint,
                context.trajectory_output_fingerprint,
            ),
        )
        mismatch = next((label for label, old, new in checks if old != new), None)
        if mismatch is not None:
            raise StaleWorkbenchSession(f"{mismatch} changed")
        if not context.can_open_workbench:
            raise StaleWorkbenchSession("workbench capability is no longer current")

    def _publish_output(
        self,
        session: WorkbenchSession,
        *,
        revision: str,
        operation_id: str,
        validated: Mapping[str, object],
    ) -> str:
        target = (
            self.outputs_root
            / validate_project_id(session.project_id)
            / "workbench_outputs"
            / revision
        )
        if target.exists():
            return self._validate_existing_output(
                target,
                session=session,
                revision=revision,
                operation_id=operation_id,
                source_revision=validated.get("source_output_revision"),
                source_fingerprint=validated.get("source_output_fingerprint"),
                source_artifact_name=validated.get("source_artifact_name"),
            )
        source_bytes = validated.get("source_bytes")
        source_name_value = validated.get("source_artifact_name")
        source_fingerprint = validated.get("source_output_fingerprint")
        if (
            not isinstance(source_bytes, bytes)
            or not isinstance(source_name_value, str)
            or Path(source_name_value).name != source_name_value
            or not isinstance(source_fingerprint, str)
            or len(source_fingerprint) != 64
        ):
            raise InvalidWorkbenchOutput(
                "validated workbench output has no authoritative source artifact"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_parent = target.parent.resolve(strict=True)
        temporary: Path | None = Path(
            tempfile.mkdtemp(prefix=f".{revision}-", dir=temporary_parent)
        ).resolve(strict=True)
        expected_prefix = f".{revision}-"
        if (
            temporary.parent != temporary_parent
            or not temporary.name.startswith(expected_prefix)
            or temporary == target.resolve(strict=False)
        ):
            temporary = None
            raise InvalidWorkbenchOutput(
                "temporary output directory escaped its owned parent"
            )
        try:
            artifacts_dir = temporary / "artifacts"
            artifacts_dir.mkdir()
            artifact_path = artifacts_dir / source_name_value
            artifact_hash = sha256(source_bytes)
            artifact_size = len(source_bytes)
            with artifact_path.open("xb") as destination_stream:
                destination_stream.write(source_bytes)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            copied_fingerprint = artifact_hash.hexdigest()
            if copied_fingerprint != source_fingerprint:
                raise InvalidWorkbenchOutput(
                    "source artifact changed while creating immutable revision"
                )
            payload = {
                "schema_version": SCHEMA_VERSION,
                "project_id": session.project_id,
                "clip_id": session.clip_id,
                "workflow": session.workflow,
                "workbench_output_revision": revision,
                "operation_id": operation_id,
                "created_at": _timestamp(self.now()),
                "source_output_revision": validated["source_output_revision"],
                "source_output_fingerprint": source_fingerprint,
                "source_artifact_name": source_name_value,
                "artifacts": {
                    "camera_track": {
                        "path": f"artifacts/{source_name_value}",
                        "sha256": copied_fingerprint,
                        "size_bytes": artifact_size,
                    }
                },
            }
            serialized = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            fingerprint = sha256(serialized).hexdigest()
            payload["workbench_output_fingerprint"] = fingerprint
            final_serialized = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            manifest = temporary / "workbench_output_manifest.json"
            with manifest.open("wb") as stream:
                stream.write(final_serialized)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(artifacts_dir)
            _fsync_directory(temporary)
            os.replace(temporary, target)
            temporary = None
            _fsync_directory(target.parent)
        finally:
            if temporary is not None and temporary.exists():
                manifest = temporary / "workbench_output_manifest.json"
                artifact = temporary / "artifacts" / source_name_value
                manifest.unlink(missing_ok=True)
                artifact.unlink(missing_ok=True)
                artifacts_directory = temporary / "artifacts"
                if artifacts_directory.exists():
                    artifacts_directory.rmdir()
                if temporary.exists():
                    temporary.rmdir()
        return fingerprint

    @staticmethod
    def _validate_existing_output(
        target: Path,
        *,
        session: WorkbenchSession,
        revision: str,
        operation_id: str,
        source_revision: object,
        source_fingerprint: object,
        source_artifact_name: object | None = None,
    ) -> str:
        manifest_path = target / "workbench_output_manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput(
                f"existing immutable output cannot be validated: {exc}"
            ) from exc
        expected = {
            "project_id": session.project_id,
            "clip_id": session.clip_id,
            "workflow": session.workflow,
            "workbench_output_revision": revision,
            "operation_id": operation_id,
            "source_output_revision": source_revision,
            "source_output_fingerprint": source_fingerprint,
        }
        if source_artifact_name is not None:
            expected["source_artifact_name"] = source_artifact_name
        if any(payload.get(key) != value for key, value in expected.items()):
            raise InvalidWorkbenchOutput(
                "existing immutable output disagrees with the pending save"
            )
        stored = payload.get("workbench_output_fingerprint")
        if not isinstance(stored, str) or not stored:
            raise InvalidWorkbenchOutput("existing immutable output has no fingerprint")
        unhashed = dict(payload)
        unhashed.pop("workbench_output_fingerprint", None)
        serialized = (
            json.dumps(unhashed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if sha256(serialized).hexdigest() != stored:
            raise InvalidWorkbenchOutput("existing immutable output fingerprint is invalid")
        artifacts = payload.get("artifacts")
        artifact = (
            artifacts.get("camera_track") if isinstance(artifacts, dict) else None
        )
        if not isinstance(artifact, dict):
            raise InvalidWorkbenchOutput("existing immutable artifact metadata is invalid")
        relative = artifact.get("path")
        artifact_fingerprint = artifact.get("sha256")
        manifest_artifact_name = payload.get("source_artifact_name")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or not isinstance(manifest_artifact_name, str)
            or Path(relative).name != manifest_artifact_name
        ):
            raise InvalidWorkbenchOutput("existing immutable artifact path is invalid")
        artifact_path = (target / relative).resolve(strict=False)
        try:
            artifact_path.relative_to(target.resolve(strict=True))
        except ValueError as exc:
            raise InvalidWorkbenchOutput("immutable artifact escaped revision") from exc
        if (
            not artifact_path.is_file()
            or not isinstance(artifact_fingerprint, str)
            or sha256(artifact_path.read_bytes()).hexdigest() != artifact_fingerprint
            or artifact_fingerprint != source_fingerprint
        ):
            raise InvalidWorkbenchOutput("existing immutable artifact fingerprint is invalid")
        return stored


class ProjectWorkbenchService:
    """Owns coordination around credential-only project and clip sessions."""

    def __init__(
        self,
        *,
        repositories: ProjectRepositories,
        project_service: ProjectService,
        session_store: WorkbenchSessionStore,
        projects_root: Path,
        viewer_runs_root: Path,
        now: Callable[[], datetime] | None = None,
        token_factory: Callable[[], str] | None = None,
        revision_factory: Callable[[], str] | None = None,
        ttl: timedelta = timedelta(minutes=30),
    ) -> None:
        self.repositories = repositories
        self.project_service = project_service
        self.projects_root = projects_root
        self.viewer_runs_root = viewer_runs_root.resolve(strict=False)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.resume_store = AtomicWorkbenchResumeStore(projects_root, now=self.now)
        self.coordinator = WorkbenchSessionCoordinator(
            store=session_store,
            outputs_root=projects_root,
            resolve_context=self.resolve_context,
            validate_output=self._validate_existing_save,
            now=self.now,
            token_factory=token_factory,
            revision_factory=revision_factory,
            ttl=ttl,
        )

    def resolve_context(self, project_id: str, clip_id: str) -> WorkbenchContext:
        project = self.repositories.project.load(project_id)
        clips = self.repositories.clips.load(project_id)
        jobs = self.repositories.jobs.load(project_id)
        clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
        if clip is None:
            raise KeyError(f"unknown clip ID: {clip_id}")
        current_job: QueueJob | None = None
        for payload in reversed(jobs.jobs):
            if payload.get("job_type") != "trajectory" or payload.get(
                "clip_id"
            ) != clip_id:
                continue
            candidate = QueueJob.from_dict(payload)
            if (
                candidate.status != "success"
                or not candidate.output_validated
                or candidate.validated_input_fingerprint
                != candidate.input_fingerprint
                or not candidate.output_revision
                or not candidate.output_fingerprint
                or candidate.adapter_name != clip.resolved_workflow
                or candidate.input_revision != clip.analysis_revision
            ):
                continue
            if (
                self.project_service._current_input_fingerprint(candidate)
                != candidate.input_fingerprint
            ):
                continue
            if not any(Path(path).is_file() for path in candidate.published_outputs.values()):
                continue
            current_job = candidate
            break
        physical_clip = self._physical_clip_for_context(project_id, clip, jobs.jobs)
        cad_design = self._cad_design_for_context(project_id, clip)
        can_open = (
            clip.resolved_workflow is not None
            and physical_clip is not None
            and cad_design is not None
        )
        return WorkbenchContext(
            project_id=project_id,
            clip_id=clip_id,
            workflow=str(clip.resolved_workflow or ""),
            project_input_revision=str(project.active_analysis_revision or ""),
            clip_input_revision=clip.analysis_revision,
            input_fingerprint=("" if current_job is None else current_job.input_fingerprint),
            trajectory_job_id=("" if current_job is None else current_job.job_id),
            trajectory_run_id=clip_id,
            trajectory_output_revision=(
                "" if current_job is None else str(current_job.output_revision)
            ),
            trajectory_output_fingerprint=(
                "" if current_job is None else str(current_job.output_fingerprint)
            ),
            save_permissions=(("save",) if current_job is not None else ()),
            can_open_workbench=can_open,
        )

    def open(
        self,
        project_id: str,
        clip_id: str,
        *,
        return_to: str,
        expected_clips_revision: int,
    ) -> WorkbenchSession:
        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(current.revision, expected_clips_revision, project_id)
            clip = next((item for item in current.clips if item.clip_id == clip_id), None)
            if clip is None:
                raise KeyError(f"unknown clip ID: {clip_id}")
            reference = self._workbench_reference(clip)
            if reference is not None and reference.value.get("status") == "stale":
                reference = None
            if reference is not None:
                token_hash = reference.value.get("session_token_hash")
                if isinstance(token_hash, str):
                    try:
                        referenced_session = self.coordinator.store.load_by_token_hash(
                            project_id, token_hash
                        )
                    except FileNotFoundError:
                        referenced_session = None
                    if (
                        referenced_session is not None
                        and referenced_session.state == "pending_save"
                    ):
                        raise WorkbenchPermissionDenied(
                            "clip has a pending workbench save that must be recovered"
                        )
                    if (
                        referenced_session is not None
                        and referenced_session.state == "saved"
                        and (
                            reference.value.get("status") != "saved"
                            or reference.operation_id
                            != referenced_session.operation_id
                            or reference.value.get("workbench_output_revision")
                            != referenced_session.workbench_output_revision
                        )
                    ):
                        raise WorkbenchPermissionDenied(
                            "clip has a saved workbench result awaiting reference repair"
                        )
            if reference is not None and reference.value.get("status") == "pending_save":
                raise WorkbenchPermissionDenied(
                    "clip has a pending workbench save that must be recovered"
                )
            if reference is not None and reference.value.get("status") == "editing":
                expires_at = reference.value.get("expires_at")
                if not isinstance(expires_at, str):
                    raise StaleWorkbenchSession(
                        "existing workbench session has invalid expiry"
                    )
                if self.now() < _parse_timestamp(expires_at):
                    raise WorkbenchPermissionDenied(
                        "clip already has an active workbench session"
                    )
            self._publish_workbench_inputs(project_id, clip)
            context = self.resolve_context(project_id, clip_id)
            resume_baseline = self._saved_resume_baseline(context)
            if (
                context.trajectory_job_id
                and context.trajectory_run_id
                and context.trajectory_output_revision
                and context.trajectory_output_fingerprint
            ):
                # Batch trajectory jobs finish before a workbench session exists.
                # Publish their validated, immutable attempt output into the
                # viewer run tree before issuing a trajectory-ready session.
                if resume_baseline is None:
                    self._materialize_trajectory_run(
                        project_id, clip, context.trajectory_job_id
                    )
                else:
                    self._restore_saved_output_to_run(
                        resume_baseline, clip, context.trajectory_job_id
                    )
            if resume_baseline is None:
                if self._scene_bridge_reference(clip) is not None:
                    self._restore_scene_bridge_to_run(project_id, clip)
                else:
                    self._restore_workbench_seed_to_run(project_id, clip)
            session = self.coordinator.create(
                project_id, clip_id, return_to=return_to
            )
            if resume_baseline is not None:
                session = self.coordinator.store.update(
                    project_id,
                    session.token,
                    expected_revision=session.revision,
                    mutate=lambda value: replace(
                        value,
                        workbench_output_revision=(
                            resume_baseline.workbench_output_revision
                        ),
                        workbench_output_fingerprint=(
                            resume_baseline.workbench_output_fingerprint
                        ),
                        workbench_output_operation_id=(
                            resume_baseline.workbench_output_operation_id
                        ),
                    ),
                )
            self._publish_clip_state(
                current,
                session,
                state="editing",
                expected_revision=expected_clips_revision,
            )
            return session

    def adjacent_location_capabilities(
        self, project_id: str, source_clip_id: str
    ) -> dict[str, object]:
        clips = self.repositories.clips.load(project_id)
        source = next(
            (item for item in clips.clips if item.clip_id == source_clip_id), None
        )
        result: dict[str, object] = {
            "can_locate_up": False,
            "can_locate_down": False,
            "locate_up_target_clip_id": None,
            "locate_down_target_clip_id": None,
            "locate_up_reason": None,
            "locate_down_reason": None,
        }
        if source is None or source.resolved_workflow != "sfm_only":
            return result
        reference = self._workbench_reference(source)
        if (
            reference is None
            or reference.value.get("status") != "saved"
            or not reference.value.get("workbench_output_revision")
        ):
            return result
        jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(project_id).jobs
        )
        source_trajectory = self.project_service._current_trajectory_for_render(
            source, jobs
        )
        if not _precomputed_scene_solve_ready(source_trajectory):
            message = "需重新轨迹反算以生成同场景重叠 SfM"
            result["locate_up_reason"] = message
            result["locate_down_reason"] = message
            return result
        for direction in ("up", "down"):
            target = self._adjacent_clip(clips.clips, source, direction)
            if target is None or not self._target_accepts_seed(
                project_id, source, target, direction
            ):
                continue
            target_trajectory = self.project_service._current_trajectory_for_render(
                target, jobs
            )
            if not _precomputed_scene_solve_ready(target_trajectory):
                result[f"locate_{direction}_reason"] = (
                    "目标片段需重新轨迹反算以生成同场景重叠 SfM"
                )
                continue
            result[f"can_locate_{direction}"] = True
            result[f"locate_{direction}_target_clip_id"] = target.clip_id
        return result

    def locate_adjacent(
        self,
        project_id: str,
        source_clip_id: str,
        *,
        direction: str,
        return_to: str,
        expected_clips_revision: int,
    ) -> tuple[WorkbenchSession | None, str]:
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(
                current.revision, expected_clips_revision, project_id
            )
            source = next(
                (item for item in current.clips if item.clip_id == source_clip_id),
                None,
            )
            if source is None:
                raise KeyError(f"unknown clip ID: {source_clip_id}")
            if source.resolved_workflow != "sfm_only":
                raise WorkbenchPermissionDenied("只有已完成路线拟合的 SfM 片段可定位相邻片段")
            target = self._adjacent_clip(current.clips, source, direction)
            if target is None:
                raise WorkbenchPermissionDenied("同场景没有可定位的相邻片段")
            if not self._target_accepts_seed(
                project_id, source, target, direction
            ):
                raise WorkbenchPermissionDenied("目标片段已有工作台会话或保存成果，不能覆盖")
            reference = self._workbench_reference(source)
            if reference is None or reference.value.get("status") != "saved":
                raise WorkbenchPermissionDenied("源片段尚未保存路线拟合成果")
            token_hash = reference.value.get("session_token_hash")
            if not isinstance(token_hash, str):
                raise InvalidWorkbenchOutput("源片段工作台凭据不可用")
            try:
                source_session = self.coordinator.store.load_by_token_hash(
                    project_id, token_hash
                )
            except FileNotFoundError as exc:
                raise InvalidWorkbenchOutput("源片段工作台成果不可用") from exc
            if source_session.state != "saved":
                raise WorkbenchPermissionDenied("源片段工作台成果尚未完成保存")
            source_manifest, source_artifact = self._validated_saved_output(
                source_session
            )
            try:
                source_track = json.loads(
                    source_artifact.read_text(encoding="utf-8-sig")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise InvalidWorkbenchOutput("源片段相机路线不可读") from exc
            anchors = confirmed_keyframes(source_track)
            if len(anchors) < 2:
                raise InvalidWorkbenchOutput("源片段没有完整的路线起点和终点")
            target_context = self.resolve_context(project_id, target.clip_id)
            if not target_context.can_open_workbench:
                if self.can_prepare(project_id, target.clip_id):
                    return None, target.clip_id
                raise WorkbenchPermissionDenied("目标片段视频或 CAD 尚未准备完成")
            source_anchor = anchors[0] if direction == "up" else anchors[-1]
            target_frame = self._target_boundary_frame(project_id, target, direction)
            fps = float(source_track.get("fps", 0.0))
            seed_track = {
                "version": int(source_track.get("version", 1)),
                "video": "",
                "fps": fps,
                "keyframes": [
                    {
                        "frame": target_frame,
                        "time": target_frame / fps,
                        "source": "scene_boundary_anchor",
                        "camera": dict(source_anchor["camera"]),
                    }
                ],
            }
            _validate_manual_camera_track(seed_track)
            existing_seed = self._workbench_seed_reference(target)
            operation_id = uuid4().hex
            seed_reference = existing_seed or self._publish_workbench_seed(
                project_id=project_id,
                source=source,
                target=target,
                direction=direction,
                target_frame=target_frame,
                source_session=source_session,
                source_manifest=source_manifest,
                seed_track=seed_track,
                operation_id=operation_id,
            )

            def publish_seed(manifest):
                updated_clips = []
                for clip in manifest.clips:
                    if clip.clip_id != target.clip_id:
                        updated_clips.append(clip)
                        continue
                    references = tuple(
                        item
                        for item in clip.references
                        if not (
                            item.owner == "clips"
                            and item.key == f"workbench_seed:{target.clip_id}"
                        )
                    )
                    updated_clips.append(
                        replace(
                            clip,
                            references=(*references, seed_reference),
                            operation_id=operation_id,
                        )
                    )
                return replace(
                    manifest,
                    clips=tuple(updated_clips),
                    operation_id=operation_id,
                )

            seeded = (
                current
                if existing_seed is not None
                else self.repositories.clips.update(
                    project_id,
                    expected_revision=current.revision,
                    mutate=publish_seed,
                )
            )
            session = self.open(
                project_id,
                target.clip_id,
                return_to=return_to,
                expected_clips_revision=seeded.revision,
            )
            return session, target.clip_id

    def inspect(self, project_id: str, token: str) -> WorkbenchSession:
        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            session = self.coordinator.store.load(project_id, token)
            clip = next(
                (item for item in current.clips if item.clip_id == session.clip_id),
                None,
            )
            if clip is None:
                raise StaleWorkbenchSession(
                    "workbench session clip no longer exists"
                )
            reference = self._workbench_reference(clip)
            if reference is None:
                raise StaleWorkbenchSession(
                    "clip has no matching workbench session"
                )
            self.coordinator._validate_binding(
                session, self.resolve_context(project_id, session.clip_id)
            )
            self._require_session_reference(
                session,
                reference,
                allow_saved_repair=session.state == "saved",
            )
            return self.coordinator.inspect(project_id, token)

    def update_resume(
        self,
        project_id: str,
        token: str,
        *,
        expected_resume_revision: int | None,
        operation_id: str,
        workflow_stage: str,
        source_pts: int,
        source_time_base: Mapping[str, object],
        quality_revision: str | None,
        render_revision: str | None,
    ) -> WorkbenchResumeState:
        """Persist browser navigation state after validating the live session."""

        session = self.inspect(project_id, token)
        stage = self._validated_resume_stage(session, workflow_stage)
        if stage not in {"quality", "render"}:
            quality_revision = None
        if stage != "render":
            render_revision = None
        return self.resume_store.update(
            project_id,
            session.clip_id,
            expected_revision=expected_resume_revision,
            operation_id=operation_id,
            workflow_stage=stage,
            source_pts=source_pts,
            source_time_base=source_time_base,
            trajectory_output_revision=(
                session.trajectory_output_revision or None
            ),
            workbench_output_revision=session.workbench_output_revision,
            quality_revision=quality_revision,
            render_revision=render_revision,
        )

    def attach_trajectory(
        self,
        project_id: str,
        token: str,
        *,
        expected_clips_revision: int,
    ) -> WorkbenchSession:
        """Bind a user-started, validated trajectory to an existing workbench session."""

        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(
                current.revision, expected_clips_revision, project_id
            )
            session = self.coordinator.store.load(project_id, token)
            if session.state != "editing" or session.launch_mode != "workflow_start":
                raise WorkbenchPermissionDenied(
                    "当前工作台会话不能绑定新的轨迹结果"
                )
            if self.coordinator._expired(session):
                raise WorkbenchPermissionDenied("工作台会话已过期")
            clip = next(
                (item for item in current.clips if item.clip_id == session.clip_id),
                None,
            )
            if clip is None:
                raise StaleWorkbenchSession("workbench session clip no longer exists")
            self._require_session_reference(
                session,
                self._workbench_reference(clip),
                allow_saved_repair=False,
            )
            context = self.resolve_context(project_id, session.clip_id)
            if (
                context.project_input_revision != session.project_input_revision
                or context.clip_input_revision != session.clip_input_revision
                or context.workflow != session.workflow
            ):
                raise StaleWorkbenchSession("workbench input changed")
            if not (
                context.trajectory_job_id
                and context.trajectory_output_revision
                and context.trajectory_output_fingerprint
                and context.save_permissions
            ):
                raise WorkbenchPermissionDenied("轨迹解算尚未成功或输出未通过验证")
            self._materialize_trajectory_run(project_id, clip, context.trajectory_job_id)
            attached = self.coordinator.store.update(
                project_id,
                token,
                expected_revision=session.revision,
                mutate=lambda value: replace(
                    value,
                    operation_id=uuid4().hex,
                    input_fingerprint=context.input_fingerprint,
                    trajectory_job_id=context.trajectory_job_id,
                    trajectory_run_id=context.trajectory_run_id,
                    trajectory_output_revision=context.trajectory_output_revision,
                    trajectory_output_fingerprint=context.trajectory_output_fingerprint,
                    launch_mode="trajectory_ready",
                    save_permissions=tuple(context.save_permissions),
                ),
            )
            self._publish_clip_state(
                current,
                attached,
                state="editing",
                expected_revision=expected_clips_revision,
            )
            return attached

    def heartbeat(
        self,
        project_id: str,
        token: str,
        *,
        expected_clips_revision: int,
    ) -> WorkbenchSession:
        """Extend an active browser-owned session without reviving stale state."""

        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(
                current.revision, expected_clips_revision, project_id
            )
            session = self.coordinator.store.load(project_id, token)
            if session.state != "editing":
                raise StaleWorkbenchSession("workbench session is not editable")
            if self.coordinator._expired(session):
                expired = self.coordinator.inspect(project_id, token)
                self._publish_clip_state(
                    current,
                    expired,
                    state="ready",
                    expected_revision=expected_clips_revision,
                )
                raise StaleWorkbenchSession("workbench session expired")
            clip = next(
                (item for item in current.clips if item.clip_id == session.clip_id),
                None,
            )
            if clip is None:
                raise StaleWorkbenchSession("workbench session clip no longer exists")
            self._require_session_reference(
                session,
                self._workbench_reference(clip),
                allow_saved_repair=False,
            )
            context = self.resolve_context(project_id, session.clip_id)
            if (
                context.project_input_revision != session.project_input_revision
                or context.clip_input_revision != session.clip_input_revision
                or context.workflow != session.workflow
                or not context.can_open_workbench
            ):
                raise StaleWorkbenchSession("workbench input changed")
            if session.launch_mode == "trajectory_ready":
                self.coordinator._validate_binding(session, context)
            operation_id = uuid4().hex
            renewed = self.coordinator.store.update(
                project_id,
                token,
                expected_revision=session.revision,
                mutate=lambda value: replace(
                    value,
                    operation_id=operation_id,
                    expires_at=_timestamp(self.now() + self.coordinator.ttl),
                ),
            )
            self._publish_clip_state(
                current,
                renewed,
                state="editing",
                expected_revision=expected_clips_revision,
            )
            return renewed

    def save(
        self,
        project_id: str,
        token: str,
        receipt: Mapping[str, object],
        *,
        expected_clips_revision: int,
    ) -> WorkbenchSession:
        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(current.revision, expected_clips_revision, project_id)
            existing_session = self.coordinator.store.load(project_id, token)
            current_clip = next(
                (
                    item
                    for item in current.clips
                    if item.clip_id == existing_session.clip_id
                ),
                None,
            )
            if current_clip is None:
                raise StaleWorkbenchSession("workbench session clip no longer exists")
            current_reference = self._workbench_reference(current_clip)
            self._require_session_reference(
                existing_session, current_reference, allow_saved_repair=True
            )
            if existing_session.state == "saved":
                self.coordinator._validate_binding(
                    existing_session,
                    self.resolve_context(project_id, existing_session.clip_id),
                )
                reference = current_reference
                token_hash = sha256(token.encode("utf-8")).hexdigest()
                if reference is not None and (
                    reference.value.get("status") == "saved"
                    and reference.operation_id == existing_session.operation_id
                    and reference.value.get("session_token_hash") == token_hash
                    and reference.value.get("workbench_output_revision")
                    == existing_session.workbench_output_revision
                ):
                    raise ReplayedWorkbenchSave(
                        "workbench session was already saved and referenced"
                    )
                if reference is not None and reference.value.get("status") == "saved":
                    raise StaleWorkbenchSession(
                        "clip has a conflicting saved workbench reference"
                    )
                if reference is not None and reference.value.get(
                    "session_token_hash"
                ) != token_hash:
                    raise StaleWorkbenchSession(
                        "clip workbench reference belongs to another session"
                    )
                saved = existing_session
            else:
                saved = self.coordinator.save(project_id, token, receipt)
            self._publish_clip_state(
                current,
                saved,
                state="saved",
                expected_revision=expected_clips_revision,
            )
            return saved

    def close(
        self,
        project_id: str,
        token: str,
        *,
        expected_clips_revision: int,
    ) -> WorkbenchSession:
        with self.project_service._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            self._require_clips_revision(current.revision, expected_clips_revision, project_id)
            before = self.coordinator.store.load(project_id, token)
            clip = next(
                (item for item in current.clips if item.clip_id == before.clip_id),
                None,
            )
            if clip is None:
                raise StaleWorkbenchSession("workbench session clip no longer exists")
            self._require_session_reference(
                before, self._workbench_reference(clip), allow_saved_repair=False
            )
            if before.state == "saved":
                self.coordinator._validate_binding(
                    before, self.resolve_context(project_id, before.clip_id)
                )
            session = self.coordinator.abandon(project_id, token)
            if (
                session.state == "ready"
                and session.workbench_output_revision
                and session.workbench_output_fingerprint
            ):
                session = self.coordinator.store.update(
                    project_id,
                    token,
                    expected_revision=session.revision,
                    mutate=lambda value: replace(
                        value,
                        state="saved",
                        operation_id=uuid4().hex,
                    ),
                )
            state = (
                session.state
                if session.state in {"saved", "pending_save"}
                else "ready"
            )
            if state == "pending_save":
                return session
            self._publish_clip_state(
                current,
                session,
                state=state,
                expected_revision=expected_clips_revision,
            )
            return session

    def snapshot_for_clip(
        self, project_id: str, clip: ClipDefinition
    ) -> dict[str, object]:
        reference = self._workbench_reference(clip)
        state = (
            "ready"
            if self.resolve_context(project_id, clip.clip_id).can_open_workbench
            else "unavailable"
        )
        output_revision = None
        if reference is not None:
            state = str(reference.value.get("status") or state)
            output_revision = reference.value.get("workbench_output_revision")
            if state == "stale":
                return {
                    "state": state,
                    "workbench_output_revision": output_revision,
                }
            credential = None
            token_hash = reference.value.get("session_token_hash")
            if isinstance(token_hash, str):
                try:
                    credential = self.coordinator.store.load_by_token_hash(
                        project_id, token_hash
                    )
                except FileNotFoundError:
                    pass
            if credential is not None and credential.state == "pending_save":
                state = "pending_save"
                output_revision = credential.pending_output_revision
            elif credential is not None and credential.state == "saved":
                output_revision = credential.workbench_output_revision
                if (
                    reference.value.get("status") == "saved"
                    and reference.operation_id == credential.operation_id
                    and reference.value.get("workbench_output_revision")
                    == credential.workbench_output_revision
                ):
                    state = "saved"
                else:
                    state = "recovery_required"
            elif credential is not None and credential.state == "editing":
                state = (
                    "ready"
                    if self.now() >= _parse_timestamp(credential.expires_at)
                    else "editing"
                )
            elif state == "editing":
                expires_at = reference.value.get("expires_at")
                if isinstance(expires_at, str) and self.now() >= _parse_timestamp(
                    expires_at
                ):
                    state = "ready"
        return {
            "state": state,
            "workbench_output_revision": output_revision,
        }

    @staticmethod
    def _workbench_reference(clip: ClipDefinition) -> StateReference | None:
        return next(
            (
                item
                for item in reversed(clip.references)
                if item.owner == "clips" and item.key == f"workbench:{clip.clip_id}"
            ),
            None,
        )

    @staticmethod
    def _workbench_seed_reference(clip: ClipDefinition) -> StateReference | None:
        return next(
            (
                item
                for item in reversed(clip.references)
                if item.owner == "clips"
                and item.key == f"workbench_seed:{clip.clip_id}"
            ),
            None,
        )

    @staticmethod
    def _scene_bridge_reference(clip: ClipDefinition) -> StateReference | None:
        return next(
            (
                item
                for item in reversed(clip.references)
                if item.owner == "jobs"
                and item.value.get("reference_type") == "scene_bridge"
                and item.value.get("target_clip_id") == clip.clip_id
                and item.value.get("status") == "awaiting_route_refinement"
            ),
            None,
        )

    @staticmethod
    def _require_session_reference(
        session: WorkbenchSession,
        reference: StateReference | None,
        *,
        allow_saved_repair: bool,
    ) -> None:
        if reference is None:
            if allow_saved_repair and session.state == "saved":
                return
            raise StaleWorkbenchSession("clip has no matching workbench session")
        if reference.value.get("status") == "stale":
            raise StaleWorkbenchSession("stale workbench reference is not repairable")
        token_hash = sha256(session.token.encode("utf-8")).hexdigest()
        value = reference.value
        checks = (
            (value.get("session_token_hash"), token_hash),
            (value.get("workflow"), session.workflow),
            (value.get("input_revision"), session.clip_input_revision),
            (value.get("input_fingerprint"), session.input_fingerprint),
            (value.get("trajectory_job_id"), session.trajectory_job_id),
            (
                value.get("trajectory_output_revision"),
                session.trajectory_output_revision,
            ),
        )
        if any(actual != expected for actual, expected in checks):
            raise StaleWorkbenchSession(
                "clip workbench reference belongs to another session or input"
            )
        if session.state == "saved" and reference.operation_id not in {
            session.operation_id,
            # The pre-save editing reference legitimately has the create operation.
        }:
            if not allow_saved_repair:
                raise StaleWorkbenchSession("saved session operation is not current")

    def can_open(self, project_id: str, clip_id: str) -> bool:
        context = self.resolve_context(project_id, clip_id)
        if not context.can_open_workbench:
            return False
        clips = self.repositories.clips.load(project_id)
        clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
        if clip is None:
            return False
        state = self.snapshot_for_clip(project_id, clip)["state"]
        return state not in {"editing", "pending_save", "recovery_required"}

    def can_prepare(self, project_id: str, clip_id: str) -> bool:
        project = self.repositories.project.load(project_id)
        clips = self.repositories.clips.load(project_id)
        clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
        if clip is None or clip.resolved_workflow is None:
            return False
        snapshot = clip.analysis.get("input_snapshot")
        video = snapshot.get("video") if isinstance(snapshot, Mapping) else None
        source_path = video.get("path") if isinstance(video, Mapping) else None
        return bool(
            source_path
            and Path(str(source_path)).is_file()
            and self._cad_design_for_context(project_id, clip) is not None
        )

    def workbench_url(self, session: WorkbenchSession) -> str:
        from urllib.parse import urlencode

        dataset = self._workbench_dataset_id(session.project_id, session.clip_id)
        parameters: dict[str, object] = {
            "dataset": dataset,
            "projectId": session.project_id,
            "runId": session.trajectory_run_id,
            "projectWorkbenchToken": session.token,
            "workflowStage": self._resume_workflow_stage(session),
            "video": f"/data/{dataset}/video/{dataset}.mp4",
            "cad": f"/data/{dataset}/cad/design.json",
            "cadScale": "0.06",
            "originXY": "0,0",
        }
        if not session.workbench_output_revision:
            clips = self.repositories.clips.load(session.project_id)
            clip = next(
                (item for item in clips.clips if item.clip_id == session.clip_id),
                None,
            )
            seed = None if clip is None else self._workbench_seed_reference(clip)
            target_frame = None if seed is None else seed.value.get("target_frame")
            if (
                isinstance(target_frame, int)
                and not isinstance(target_frame, bool)
                and target_frame >= 0
            ):
                parameters["initialFrame"] = target_frame
        return "/apps/web_camera_viewer/?" + urlencode(parameters)

    def session_payload(self, session: WorkbenchSession) -> dict[str, object]:
        return {
            "token": session.token,
            "project_id": session.project_id,
            "clip_id": session.clip_id,
            "workflow": session.workflow,
            "trajectory_job_id": session.trajectory_job_id,
            "trajectory_run_id": session.trajectory_run_id,
            "launch_mode": session.launch_mode,
            "state": session.state,
            "expires_at": session.expires_at,
            "return_to": session.return_to,
            "save_permissions": list(session.save_permissions),
            "session_revision": session.revision,
            "workbench_output_revision": session.workbench_output_revision,
            "resume_state": self._resume_payload(session),
        }

    def _resume_payload(self, session: WorkbenchSession) -> dict[str, object] | None:
        state = self.resume_store.load_optional(session.project_id, session.clip_id)
        if state is None:
            return None
        payload = state.to_dict()
        payload["workflow_stage"] = self._validated_resume_stage(
            session, state.workflow_stage, state=state
        )
        return payload

    def _resume_workflow_stage(self, session: WorkbenchSession) -> str:
        resume = self._resume_payload(session)
        if resume is not None:
            return str(resume["workflow_stage"])
        if session.workbench_output_revision and session.workbench_output_fingerprint:
            return "render"
        return "keyframes" if session.launch_mode == "trajectory_ready" else "sfm"

    def _validated_resume_stage(
        self,
        session: WorkbenchSession,
        requested: str,
        *,
        state: WorkbenchResumeState | None = None,
    ) -> str:
        if requested not in {"sfm", "keyframes", "quality", "render"}:
            raise ValueError(f"unsupported workflow_stage: {requested}")
        trajectory_ready = bool(
            session.launch_mode == "trajectory_ready"
            and session.trajectory_output_revision
            and session.trajectory_output_fingerprint
        )
        if state is not None and state.trajectory_output_revision != (
            session.trajectory_output_revision or None
        ):
            requested = "keyframes" if trajectory_ready else "sfm"
        if not trajectory_ready:
            return "sfm"
        if requested in {"sfm", "keyframes"}:
            return "keyframes"
        if not self._has_fitted_track(session):
            return "keyframes"
        if requested == "quality":
            return "quality"
        if (
            session.workbench_output_revision
            and session.workbench_output_fingerprint
            and (
                state is None
                or state.workbench_output_revision
                == session.workbench_output_revision
            )
        ):
            return "render"
        return "quality"

    def _has_fitted_track(self, session: WorkbenchSession) -> bool:
        relative_candidates = (
            Path("03_alignment/camera_track_pred.json"),
            Path("03_pure_rotation_placement/camera_track_cad_base.json"),
        )
        return any(
            (root / relative).is_file()
            for root in self._bound_workbench_run_roots(session)
            for relative in relative_candidates
        )

    @staticmethod
    def _adjacent_clip(
        clips: tuple[ClipDefinition, ...],
        source: ClipDefinition,
        direction: str,
    ) -> ClipDefinition | None:
        scene_index = int(source.analysis.get("scene_index", 1))
        same_scene = sorted(
            (
                clip
                for clip in clips
                if int(clip.analysis.get("scene_index", 1)) == scene_index
            ),
            key=lambda clip: (
                int(clip.analysis.get("segment_index", 1)),
                int(clip.analysis.get("render_order", 0)),
                clip.clip_id,
            ),
        )
        source_index = next(
            (
                index
                for index, clip in enumerate(same_scene)
                if clip.clip_id == source.clip_id
            ),
            None,
        )
        if source_index is None:
            return None
        target_index = source_index - 1 if direction == "up" else source_index + 1
        return same_scene[target_index] if 0 <= target_index < len(same_scene) else None

    def _target_accepts_seed(
        self,
        project_id: str,
        source: ClipDefinition,
        target: ClipDefinition,
        direction: str,
    ) -> bool:
        reference = self._workbench_reference(target)
        if reference is not None:
            status = reference.value.get("status")
            if (
                status in {"pending_save", "saved"}
                or reference.value.get("workbench_output_revision")
            ):
                return False
            if status == "editing" and self.snapshot_for_clip(
                project_id, target
            ).get("state") != "ready":
                return False
        context = self.resolve_context(project_id, target.clip_id)
        if self._saved_resume_baseline(context) is not None:
            return False
        seed = self._workbench_seed_reference(target)
        if seed is not None:
            source_reference = self._workbench_reference(source)
            if (
                source_reference is None
                or seed.value.get("source_clip_id") != source.clip_id
                or seed.value.get("direction") != direction
                or seed.value.get("source_workbench_output_revision")
                != source_reference.value.get("workbench_output_revision")
                or seed.value.get("source_workbench_output_fingerprint")
                != source_reference.value.get("workbench_output_fingerprint")
            ):
                return False
            try:
                self._validated_workbench_seed(project_id, target, seed)
            except (InvalidWorkbenchOutput, OSError, ValueError, TypeError):
                return False
        return context.can_open_workbench or self.can_prepare(
            project_id, target.clip_id
        )

    def _target_boundary_frame(
        self, project_id: str, target: ClipDefinition, direction: str
    ) -> int:
        if direction == "down":
            return 0
        jobs = self.repositories.jobs.load(project_id)
        frame_map = self._frame_map_for_context(project_id, target, jobs.jobs)
        if frame_map is None:
            raise WorkbenchPermissionDenied("上一片段缺少权威 frame map，无法定位末帧")
        try:
            payload = json.loads(frame_map.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput("目标片段 frame map 不可读") from exc
        frames = payload.get("frames") if isinstance(payload, Mapping) else None
        if not isinstance(frames, list) and isinstance(payload, Mapping):
            clip_rows = payload.get("clips")
            selected = (
                next(
                    (
                        row
                        for row in clip_rows
                        if isinstance(row, Mapping)
                        and row.get("clip_id") == target.clip_id
                    ),
                    None,
                )
                if isinstance(clip_rows, list)
                else None
            )
            frames = selected.get("frames") if isinstance(selected, Mapping) else None
        if (
            not isinstance(frames, list)
            or not frames
            or not isinstance(frames[-1], Mapping)
        ):
            raise InvalidWorkbenchOutput("目标片段 frame map 没有帧记录")
        frame = frames[-1].get(
            "output_frame_ordinal",
            frames[-1].get("ordinal", len(frames) - 1),
        )
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise InvalidWorkbenchOutput("目标片段末帧序号无效")
        return frame

    def _frame_map_for_context(
        self,
        project_id: str,
        clip: ClipDefinition,
        job_payloads: tuple[Mapping[str, object], ...],
    ) -> Path | None:
        for key in ("frame_map_path", "clip_frame_map_path"):
            value = clip.analysis.get(key)
            if value and Path(str(value)).is_file():
                return Path(str(value))
        for payload in reversed(job_payloads):
            if (
                payload.get("job_type") != "clip_export"
                or payload.get("clip_id") != clip.clip_id
            ):
                continue
            candidate = QueueJob.from_dict(payload)
            frame_map = candidate.published_outputs.get(f"frame_map:{clip.clip_id}")
            if (
                candidate.status == "success"
                and candidate.output_validated
                and candidate.validated_input_fingerprint == candidate.input_fingerprint
                and self.project_service._current_input_fingerprint(candidate)
                == candidate.input_fingerprint
                and isinstance(frame_map, str)
                and Path(frame_map).is_file()
            ):
                return Path(frame_map)
        return None

    def _publish_workbench_seed(
        self,
        *,
        project_id: str,
        source: ClipDefinition,
        target: ClipDefinition,
        direction: str,
        target_frame: int,
        source_session: WorkbenchSession,
        source_manifest: Mapping[str, object],
        seed_track: Mapping[str, object],
        operation_id: str,
    ) -> StateReference:
        track_bytes = (
            json.dumps(seed_track, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        track_hash = sha256(track_bytes).hexdigest()
        identity = {
            "project_id": project_id,
            "source_clip_id": source.clip_id,
            "target_clip_id": target.clip_id,
            "direction": direction,
            "target_frame": target_frame,
            "source_workbench_output_revision": source_session.workbench_output_revision,
            "source_workbench_output_fingerprint": source_session.workbench_output_fingerprint,
            "source_output_revision": source_manifest.get("source_output_revision"),
            "track_sha256": track_hash,
            "operation_id": operation_id,
        }
        seed_hash = sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        revision = f"seed-{seed_hash[:16]}"
        parent = self.projects_root / project_id / "workbench_seeds" / target.clip_id
        parent.mkdir(parents=True, exist_ok=True)
        target_root = parent / revision
        temporary = Path(tempfile.mkdtemp(prefix=f".{revision}-", dir=parent))
        try:
            track_path = temporary / "camera_track_seed.json"
            manifest_path = temporary / "workbench_seed_manifest.json"
            _atomic_write_bytes(track_path, track_bytes)
            manifest = {
                "schema_version": 1,
                "seed_revision": revision,
                **identity,
                "camera_track": {
                    "path": "camera_track_seed.json",
                    "sha256": track_hash,
                    "size_bytes": len(track_bytes),
                },
            }
            _atomic_write_bytes(
                manifest_path,
                (
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8"),
            )
            os.replace(temporary, target_root)
            _fsync_directory(parent)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
        return StateReference(
            owner="clips",
            key=f"workbench_seed:{target.clip_id}",
            operation_id=operation_id,
            value={
                "seed_revision": revision,
                "source_clip_id": source.clip_id,
                "source_workbench_output_revision": source_session.workbench_output_revision,
                "source_workbench_output_fingerprint": source_session.workbench_output_fingerprint,
                "direction": direction,
                "target_frame": target_frame,
                "track_sha256": track_hash,
            },
        )

    def _restore_workbench_seed_to_run(
        self, project_id: str, clip: ClipDefinition
    ) -> None:
        reference = self._workbench_seed_reference(clip)
        if reference is None:
            return
        track_bytes = self._validated_workbench_seed(project_id, clip, reference)
        dataset = self._workbench_dataset_id(project_id, clip.clip_id)
        destination = (
            self.viewer_runs_root
            / dataset
            / clip.clip_id
            / "01_keyframes/camera_track_manual.json"
        )
        _atomic_write_bytes(destination, track_bytes)

    def _restore_scene_bridge_to_run(
        self, project_id: str, clip: ClipDefinition
    ) -> None:
        reference = self._scene_bridge_reference(clip)
        if reference is None:
            return
        revision = reference.value.get("bridge_revision")
        if not isinstance(revision, str) or not revision.startswith("bridge-"):
            raise InvalidWorkbenchOutput("场景路线桥接 revision 无效")
        root = (
            self.projects_root
            / project_id
            / "scene_bridges"
            / clip.clip_id
            / revision
        )
        manifest_path = root / "scene_bridge_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput("场景路线桥接结果不可读") from exc
        identity = manifest.get("identity") if isinstance(manifest, Mapping) else None
        if not isinstance(identity, Mapping):
            raise InvalidWorkbenchOutput("场景路线桥接身份缺失")
        validated = validate_scene_bridge_candidate(root, identity)
        if (
            validated.status != "success"
            or validated.output_revision != revision
            or validated.output_fingerprint
            != reference.value.get("output_fingerprint")
            or identity.get("project_id") != project_id
            or identity.get("target_clip_id") != clip.clip_id
            or identity.get("source_clip_id")
            != reference.value.get("source_clip_id")
            or identity.get("direction") != reference.value.get("direction")
        ):
            raise InvalidWorkbenchOutput("场景路线桥接结果校验失败")
        dataset = self._workbench_dataset_id(project_id, clip.clip_id)
        run = self.viewer_runs_root / dataset / clip.clip_id
        _atomic_write_bytes(
            run / "01_keyframes/camera_track_manual.json",
            (root / "camera_track_seed.json").read_bytes(),
        )
        for stage in ("03_alignment", "05_viewer_scene"):
            source = root / "core_alignment" / stage
            destination = run / stage
            temporary = run / f".{stage}.{uuid4().hex}.tmp"
            stale = run / f".{stage}.{uuid4().hex}.stale"
            shutil.copytree(source, temporary)
            moved_old = False
            try:
                if destination.exists():
                    os.replace(destination, stale)
                    moved_old = True
                os.replace(temporary, destination)
                _fsync_directory(run)
            except Exception:
                if moved_old and stale.exists() and not destination.exists():
                    os.replace(stale, destination)
                raise
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
                if stale.exists():
                    shutil.rmtree(stale)

    def _validated_workbench_seed(
        self,
        project_id: str,
        clip: ClipDefinition,
        reference: StateReference,
    ) -> bytes:
        revision = reference.value.get("seed_revision")
        if not isinstance(revision, str) or not revision.startswith("seed-"):
            raise InvalidWorkbenchOutput("工作台定位种子 revision 无效")
        root = (
            self.projects_root
            / project_id
            / "workbench_seeds"
            / clip.clip_id
            / revision
        )
        manifest_path = root / "workbench_seed_manifest.json"
        track_path = root / "camera_track_seed.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            track_bytes = track_path.read_bytes()
            track = json.loads(track_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput("工作台定位种子不可读") from exc
        expected = {
            "schema_version": 1,
            "seed_revision": revision,
            "project_id": project_id,
            "target_clip_id": clip.clip_id,
            "source_clip_id": reference.value.get("source_clip_id"),
            "direction": reference.value.get("direction"),
            "target_frame": reference.value.get("target_frame"),
            "source_workbench_output_revision": reference.value.get(
                "source_workbench_output_revision"
            ),
            "source_workbench_output_fingerprint": reference.value.get(
                "source_workbench_output_fingerprint"
            ),
            "track_sha256": reference.value.get("track_sha256"),
            "operation_id": reference.operation_id,
        }
        if not isinstance(manifest, Mapping) or any(
            manifest.get(key) != value for key, value in expected.items()
        ):
            raise InvalidWorkbenchOutput("工作台定位种子身份不匹配")
        if sha256(track_bytes).hexdigest() != reference.value.get("track_sha256"):
            raise InvalidWorkbenchOutput("工作台定位种子内容校验失败")
        _validate_manual_camera_track(track)
        return track_bytes

    def _physical_clip_for_context(
        self,
        project_id: str,
        clip: ClipDefinition,
        job_payloads: tuple[Mapping[str, object], ...],
    ) -> Path | None:
        for key in ("physical_mp4_path", "export_path", "clip_path"):
            value = clip.analysis.get(key)
            if value and Path(str(value)).is_file():
                return Path(str(value))
        for payload in reversed(job_payloads):
            if (
                payload.get("job_type") != "clip_export"
                or payload.get("clip_id") != clip.clip_id
            ):
                continue
            candidate = QueueJob.from_dict(payload)
            video = candidate.published_outputs.get(f"video:{clip.clip_id}")
            if (
                candidate.status == "success"
                and candidate.output_validated
                and candidate.validated_input_fingerprint == candidate.input_fingerprint
                and self.project_service._current_input_fingerprint(candidate)
                == candidate.input_fingerprint
                and video
                and Path(video).is_file()
            ):
                return Path(video)
        return None

    @staticmethod
    def _workbench_dataset_id(project_id: str, clip_id: str) -> str:
        return slugify_dataset_name(f"{validate_project_id(project_id)}-{clip_id}")

    def _cad_design_for_context(
        self, project_id: str, clip: ClipDefinition
    ) -> Path | None:
        project = self.repositories.project.load(project_id)
        active = project.source_assets.get("cad")
        active_dataset = (
            active.get("dataset_path") if isinstance(active, Mapping) else None
        )
        if active_dataset:
            design = Path(str(active_dataset)) / "design.json"
            if design.is_file():
                return design
        snapshot = clip.analysis.get("input_snapshot")
        if not isinstance(snapshot, Mapping):
            return None
        cad = snapshot.get("cad")
        if not isinstance(cad, Mapping):
            return None
        dataset_path = cad.get("dataset_path")
        if not dataset_path:
            return None
        design = Path(str(dataset_path)) / "design.json"
        return design if design.is_file() else None

    def _publish_workbench_inputs(
        self, project_id: str, clip: ClipDefinition
    ) -> None:
        jobs = self.repositories.jobs.load(project_id)
        video = self._physical_clip_for_context(project_id, clip, jobs.jobs)
        cad = self._cad_design_for_context(project_id, clip)
        if video is None:
            raise WorkbenchPermissionDenied("片段视频尚未准备完成")
        if cad is None:
            raise WorkbenchPermissionDenied("项目 CAD 尚未准备完成")
        storage_root = self.viewer_runs_root.parent.resolve(strict=True)
        data_root = (storage_root / "data").resolve(strict=False)
        dataset = self._workbench_dataset_id(project_id, clip.clip_id)
        target = (data_root / dataset).resolve(strict=False)
        if data_root not in (target, *target.parents):
            raise WorkbenchPermissionDenied("工作台资源目录无效")
        video_target = target / "video" / f"{dataset}.mp4"
        cad_target = target / "cad" / "design.json"
        for source, destination in ((video, video_target), (cad, cad_target)):
            _publish_readonly_file(source, destination)
        manifest = {
            "dataset": dataset,
            "status": "ready",
            "video": {
                "path": f"data/{dataset}/video/{dataset}.mp4",
                "url": f"/data/{dataset}/video/{dataset}.mp4",
            },
            "cad": {
                "status": "ready",
                "design_json": f"data/{dataset}/cad/design.json",
                "url": f"/data/{dataset}/cad/design.json",
            },
            "workflow": {
                "trajectory_mode": str(clip.resolved_workflow),
                "implementation_status": "ready",
            },
            "defaults": {"cad_scale": 0.06, "origin_xy": [0.0, 0.0]},
            "project_binding": {
                "project_id": project_id,
                "clip_id": clip.clip_id,
                "analysis_revision": clip.analysis_revision,
            },
        }
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / "dataset_manifest.json"
        temporary_manifest = target / f".dataset_manifest.{uuid4().hex}.tmp"
        payload = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        try:
            with temporary_manifest.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_manifest, manifest_path)
            _fsync_directory(target)
        finally:
            if temporary_manifest.exists():
                temporary_manifest.unlink()

    def _materialize_trajectory_run(
        self, project_id: str, clip: ClipDefinition, job_id: str
    ) -> None:
        jobs = self.repositories.jobs.load(project_id)
        payload = next(
            (item for item in jobs.jobs if item.get("job_id") == job_id), None
        )
        if payload is None:
            raise WorkbenchPermissionDenied("找不到已完成的轨迹任务")
        job = QueueJob.from_dict(payload)
        if not job.attempts:
            raise WorkbenchPermissionDenied("轨迹任务缺少输出目录")
        source = (
            Path(job.attempts[-1].directory) / project_id / clip.clip_id
        ).resolve(strict=False)
        if not source.is_dir():
            raise WorkbenchPermissionDenied("轨迹任务输出目录不存在")
        trajectory = job.published_outputs.get("trajectory")
        if not trajectory or not Path(trajectory).is_file():
            raise WorkbenchPermissionDenied("轨迹任务输出未通过验证")
        try:
            Path(trajectory).resolve(strict=True).relative_to(source.resolve(strict=True))
        except ValueError as exc:
            raise WorkbenchPermissionDenied("轨迹任务输出不属于当前片段") from exc
        dataset = self._workbench_dataset_id(project_id, clip.clip_id)
        target = (self.viewer_runs_root / dataset / clip.clip_id).resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        stale = target.parent / f".{target.name}.{uuid4().hex}.stale"
        shutil.copytree(source, temporary)
        moved_old = False
        committed = False
        try:
            if target.exists():
                os.replace(target, stale)
                moved_old = True
            os.replace(temporary, target)
            _fsync_directory(target.parent)
            committed = True
        except Exception:
            if moved_old and stale.exists() and not target.exists():
                os.replace(stale, target)
            raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
            if committed and stale.exists():
                shutil.rmtree(stale)

    def _saved_resume_baseline(
        self, context: WorkbenchContext
    ) -> WorkbenchSession | None:
        candidates = reversed(
            self.coordinator.store.list_for_project(context.project_id)
        )
        for candidate in candidates:
            if candidate.state != "saved":
                continue
            try:
                self.coordinator._validate_binding(candidate, context)
                self._validated_saved_output(candidate)
            except (WorkbenchSessionError, OSError, ValueError, KeyError, TypeError):
                continue
            return candidate
        return None

    def _validated_saved_output(
        self, session: WorkbenchSession
    ) -> tuple[Mapping[str, object], Path]:
        revision = session.workbench_output_revision
        fingerprint = session.workbench_output_fingerprint
        if not revision or not fingerprint:
            raise InvalidWorkbenchOutput("saved session has no immutable output")
        target = (
            self.projects_root
            / validate_project_id(session.project_id)
            / "workbench_outputs"
            / revision
        )
        manifest_path = target / "workbench_output_manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput(
                "saved workbench output manifest is unavailable"
            ) from exc
        stored = self.coordinator._validate_existing_output(
            target,
            session=session,
            revision=revision,
            operation_id=(
                session.workbench_output_operation_id or session.operation_id
            ),
            source_revision=payload.get("source_output_revision"),
            source_fingerprint=payload.get("source_output_fingerprint"),
            source_artifact_name=payload.get("source_artifact_name"),
        )
        if stored != fingerprint:
            raise InvalidWorkbenchOutput(
                "saved session output fingerprint does not match its manifest"
            )
        artifacts = payload.get("artifacts")
        camera_track = (
            artifacts.get("camera_track") if isinstance(artifacts, Mapping) else None
        )
        relative = camera_track.get("path") if isinstance(camera_track, Mapping) else None
        if not isinstance(relative, str):
            raise InvalidWorkbenchOutput("saved camera track path is unavailable")
        artifact = (target / relative).resolve(strict=True)
        try:
            artifact.relative_to(target.resolve(strict=True))
        except ValueError as exc:
            raise InvalidWorkbenchOutput("saved camera track escaped its revision") from exc
        return payload, artifact

    def _restore_saved_output_to_run(
        self,
        session: WorkbenchSession,
        clip: ClipDefinition,
        trajectory_job_id: str,
    ) -> None:
        payload, artifact = self._validated_saved_output(session)
        artifact_bytes = artifact.read_bytes()
        dataset = self._workbench_dataset_id(session.project_id, session.clip_id)
        run = self.viewer_runs_root / dataset / session.clip_id
        source_name = str(payload.get("source_artifact_name") or "")
        if session.workflow == "pure_rotation":
            existing = (
                run
                / (
                    "04_pure_rotation_corrections/camera_track_corrected.json"
                    if source_name == "camera_track_corrected.json"
                    else "03_pure_rotation_placement/camera_track_cad_base.json"
                )
            )
            placement = run / "03_pure_rotation_placement/global_camera_placement.json"
            if (
                existing.is_file()
                and sha256(existing.read_bytes()).digest()
                == sha256(artifact_bytes).digest()
                and placement.is_file()
            ):
                return
            self._materialize_trajectory_run(
                session.project_id, clip, trajectory_job_id
            )
            base = run / "03_pure_rotation_placement/camera_track_cad_base.json"
            _atomic_write_bytes(base, artifact_bytes)
            track = json.loads(artifact_bytes.decode("utf-8-sig"))
            poses = track.get("poses") if isinstance(track, Mapping) else None
            first = poses[0] if isinstance(poses, list) and poses else None
            if not isinstance(first, Mapping):
                raise InvalidWorkbenchOutput("saved camera track contains no poses")
            placement_payload = {
                "segment_id": int(first.get("segment_id", 0)),
                "anchor_decoded_frame_index": int(
                    first.get("decoded_frame_index", first.get("frame_index", 0))
                ),
                "anchor_pts_time_sec": float(first.get("pts_time_sec", 0.0)),
                "camera_center_web": [
                    float(value)
                    for value in first.get("camera_center_web", [0.0, 0.0, 0.0])
                ],
                "manual_rotation_cad_from_camera": first.get(
                    "rotation_cad_from_camera",
                    [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                ),
                "fov": float(track.get("display_fov", 50.0)),
                "restored_from_workbench_output_revision": (
                    session.workbench_output_revision
                ),
            }
            _atomic_write_bytes(
                placement,
                (json.dumps(placement_payload, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                ),
            )
            return
        manual = run / "01_keyframes/camera_track_manual.json"
        if (
            manual.is_file()
            and sha256(manual.read_bytes()).digest() == sha256(artifact_bytes).digest()
        ):
            return
        self._materialize_trajectory_run(session.project_id, clip, trajectory_job_id)
        _atomic_write_bytes(manual, artifact_bytes)

    def _publish_clip_state(
        self,
        current,
        session: WorkbenchSession,
        *,
        state: str,
        expected_revision: int,
    ) -> None:
        token_hash = sha256(session.token.encode("utf-8")).hexdigest()

        def mutate(manifest):
            clips: list[ClipDefinition] = []
            found = False
            for clip in manifest.clips:
                if clip.clip_id != session.clip_id:
                    clips.append(clip)
                    continue
                found = True
                references = tuple(
                    item
                    for item in clip.references
                    if not (
                        item.owner == "clips"
                        and item.key == f"workbench:{clip.clip_id}"
                    )
                )
                reference = StateReference(
                    owner="clips",
                    key=f"workbench:{clip.clip_id}",
                    operation_id=session.operation_id,
                    value={
                        "status": state,
                        "session_token_hash": token_hash,
                        "workflow": session.workflow,
                        "input_revision": session.clip_input_revision,
                        "input_fingerprint": session.input_fingerprint,
                        "trajectory_job_id": session.trajectory_job_id,
                        "trajectory_output_revision": session.trajectory_output_revision,
                        "trajectory_output_fingerprint": (
                            session.trajectory_output_fingerprint
                        ),
                        "expires_at": session.expires_at,
                        "workbench_output_revision": session.workbench_output_revision,
                        "workbench_output_fingerprint": session.workbench_output_fingerprint,
                        "workbench_output_operation_id": (
                            session.workbench_output_operation_id
                        ),
                    },
                )
                clips.append(
                    replace(
                        clip,
                        references=(*references, reference),
                        operation_id=session.operation_id,
                    )
                )
            if not found:
                raise KeyError(f"unknown clip ID: {session.clip_id}")
            return replace(
                manifest,
                clips=tuple(clips),
                operation_id=session.operation_id,
            )

        self.repositories.clips.update(
            current.project_id,
            expected_revision=expected_revision,
            mutate=mutate,
        )

    @staticmethod
    def _require_clips_revision(
        current: int, expected: int, project_id: str
    ) -> None:
        if current != expected:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected,
                current_revision=current,
            )

    def _validate_existing_save(
        self, session: WorkbenchSession, receipt: Mapping[str, object]
    ) -> Mapping[str, object]:
        if receipt.get("ok") is not True:
            raise InvalidWorkbenchOutput("existing workbench save did not succeed")
        if session.workflow == "pure_rotation":
            return self._validate_pure_rotation_save(session, receipt)
        supplied = receipt.get("path")
        if not isinstance(supplied, str) or not supplied:
            raise InvalidWorkbenchOutput("existing workbench save path is missing")
        expected_paths = tuple(
            (
                root / "01_keyframes" / "camera_track_manual.json"
            ).resolve(strict=False)
            for root in self._bound_workbench_run_roots(session)
        )
        actual = Path(supplied).resolve(strict=False)
        if actual not in expected_paths:
            raise InvalidWorkbenchOutput("path is not the bound workbench output")
        if not actual.is_file():
            raise InvalidWorkbenchOutput("bound workbench output is missing")
        try:
            source_bytes = actual.read_bytes()
            payload = json.loads(source_bytes.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput(f"invalid bound workbench output: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("keyframes"), list):
            raise InvalidWorkbenchOutput("bound workbench output contract is invalid")
        _validate_manual_camera_track(payload)
        fingerprint = sha256(source_bytes).hexdigest()
        return {
            "source_output_revision": f"manual-track:{fingerprint[:16]}",
            "source_output_fingerprint": fingerprint,
            "source_path": str(actual),
            "source_bytes": source_bytes,
            "source_artifact_name": actual.name,
        }

    def _validate_pure_rotation_save(
        self, session: WorkbenchSession, receipt: Mapping[str, object]
    ) -> Mapping[str, object]:
        if receipt.get("kind") != "pure_rotation_calibration":
            raise InvalidWorkbenchOutput(
                "pure-rotation workbench save receipt is invalid"
            )
        roots = self._bound_workbench_run_roots(session)
        run_root = next(
            (
                root
                for root in roots
                if (
                    root
                    / "03_pure_rotation_placement"
                    / "camera_track_cad_base.json"
                ).is_file()
            ),
            roots[0],
        )
        try:
            run_root.relative_to(self.viewer_runs_root)
        except ValueError as exc:
            raise InvalidWorkbenchOutput("pure-rotation run path escaped root") from exc
        with pure_rotation_run_lock(run_root):
            return self._validate_pure_rotation_run_snapshot(run_root)

    def _bound_workbench_run_roots(
        self, session: WorkbenchSession
    ) -> tuple[Path, ...]:
        bridge = (
            self.viewer_runs_root
            / self._workbench_dataset_id(session.project_id, session.clip_id)
            / session.trajectory_run_id
        ).resolve(strict=False)
        legacy = (
            self.viewer_runs_root / session.project_id / session.trajectory_run_id
        ).resolve(strict=False)
        roots = (bridge, legacy)
        try:
            for root in roots:
                root.relative_to(self.viewer_runs_root)
        except ValueError as exc:
            raise InvalidWorkbenchOutput("workbench run path escaped root") from exc
        return roots

    @staticmethod
    def _validate_pure_rotation_run_snapshot(
        run_root: Path,
    ) -> Mapping[str, object]:
        base = (
            run_root
            / "03_pure_rotation_placement"
            / "camera_track_cad_base.json"
        )
        corrected = (
            run_root
            / "04_pure_rotation_corrections"
            / "camera_track_corrected.json"
        )
        lineage = corrected.parent / "correction_lineage.json"
        try:
            base_bytes = base.read_bytes()
        except OSError:
            base_bytes = None
        try:
            corrected_bytes = corrected.read_bytes()
        except OSError:
            corrected_bytes = None
        try:
            lineage_bytes = lineage.read_bytes()
        except OSError:
            lineage_bytes = None
        actual = base if base_bytes is not None else None
        actual_bytes = base_bytes
        if base_bytes is not None and corrected_bytes is not None and lineage_bytes is not None:
            try:
                lineage_payload = json.loads(lineage_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                lineage_payload = {}
            if (
                isinstance(lineage_payload, dict)
                and lineage_payload.get("base_sha256")
                == sha256(base_bytes).hexdigest()
                and lineage_payload.get("corrected_sha256")
                == sha256(corrected_bytes).hexdigest()
            ):
                actual = corrected
                actual_bytes = corrected_bytes
        if actual is None or actual_bytes is None:
            raise InvalidWorkbenchOutput(
                "validated pure-rotation workbench output is missing"
            )
        try:
            payload = json.loads(actual_bytes.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput(
                f"invalid pure-rotation workbench output: {exc}"
            ) from exc
        poses = payload.get("poses") if isinstance(payload, dict) else None
        if (
            not isinstance(poses, list)
            or not poses
            or payload.get("trajectory_mode")
            != "pure_rotation_manual_calibrated"
            or any(
                not isinstance(pose, dict)
                or "decoded_frame_index" not in pose
                or not _valid_rotation_matrix(pose.get("rotation_cad_from_camera"))
                or not _valid_vector3(pose.get("camera_center_web"))
                for pose in poses
            )
        ):
            raise InvalidWorkbenchOutput(
                "pure-rotation workbench output contract is invalid"
            )
        fingerprint = sha256(actual_bytes).hexdigest()
        return {
            "source_output_revision": f"pure-track:{fingerprint[:16]}",
            "source_output_fingerprint": fingerprint,
            "source_path": str(actual),
            "source_bytes": actual_bytes,
            "source_artifact_name": actual.name,
        }


def validate_return_to(return_to: str, project_id: str, clip_id: str) -> str:
    if not isinstance(return_to, str) or not return_to or "\\" in return_to:
        raise InvalidWorkbenchReturnPath("return_to must be a local workspace path")
    decoded = return_to
    for _ in range(4):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if "\\" in decoded:
        raise InvalidWorkbenchReturnPath("return_to contains a backslash")
    parsed = urlsplit(decoded)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise InvalidWorkbenchReturnPath("return_to must be same-origin")
    segments = parsed.path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise InvalidWorkbenchReturnPath("return_to contains traversal")
    if parsed.path != "/apps/project_workspace/":
        raise InvalidWorkbenchReturnPath("return_to path is not allowlisted")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query.get("projectId") != [project_id]:
        raise InvalidWorkbenchReturnPath("return_to project binding is invalid")
    if "focusClip" in query and query["focusClip"] != [clip_id]:
        raise InvalidWorkbenchReturnPath("return_to clip binding is invalid")
    if set(query) - {"projectId", "focusClip"}:
        raise InvalidWorkbenchReturnPath("return_to query is not allowlisted")
    return return_to


def _validate_context_identity(
    context: WorkbenchContext, project_id: str, clip_id: str
) -> None:
    if context.project_id != project_id or context.clip_id != clip_id:
        raise StaleWorkbenchSession("resolved workbench context identity mismatch")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("workbench session clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _receipt_fingerprint(receipt: Mapping[str, object]) -> str:
    try:
        serialized = json.dumps(
            dict(receipt),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidWorkbenchOutput(
            "workbench save receipt is not canonical JSON"
        ) from exc
    return sha256(serialized).hexdigest()


def _valid_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _valid_vector3(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(_valid_number(item) for item in value)
    )


def _valid_rotation_matrix(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(_valid_vector3(row) for row in value)
    )


def _validate_manual_camera_track(payload: Mapping[str, object]) -> None:
    fps = payload.get("fps")
    keyframes = payload.get("keyframes")
    if not _valid_number(fps) or float(fps) <= 0:
        raise InvalidWorkbenchOutput("camera track fps must be finite and positive")
    if not isinstance(keyframes, list) or not keyframes:
        raise InvalidWorkbenchOutput("camera track must contain keyframes")
    frames: set[int] = set()
    camera_fields = ("x", "y", "z", "yaw", "pitch", "roll", "fov")
    for keyframe in keyframes:
        if not isinstance(keyframe, dict):
            raise InvalidWorkbenchOutput("camera track keyframe must be an object")
        frame = keyframe.get("frame")
        if (
            isinstance(frame, bool)
            or not isinstance(frame, int)
            or frame < 0
            or frame in frames
        ):
            raise InvalidWorkbenchOutput(
                "camera track frame must be a unique non-negative integer"
            )
        frames.add(frame)
        if "time" in keyframe and not _valid_number(keyframe.get("time")):
            raise InvalidWorkbenchOutput("camera track keyframe time must be finite")
        camera = keyframe.get("camera")
        if not isinstance(camera, dict) or any(
            not _valid_number(camera.get(field)) for field in camera_fields
        ):
            raise InvalidWorkbenchOutput(
                "camera track keyframe camera must contain finite pose values"
            )
        if not 1.0 < float(camera["fov"]) < 179.0:
            raise InvalidWorkbenchOutput("camera track camera fov is invalid")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _publish_readonly_file(source: Path, destination: Path) -> None:
    """优先硬链接不可变输入，跨卷或权限不允许时保持原子复制。"""

    source = source.resolve(strict=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        try:
            if os.path.samefile(source, destination):
                return
        except OSError:
            pass
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copy2(source, temporary)
            with temporary.open("rb+") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _precomputed_scene_solve_ready(job: QueueJob | None) -> bool:
    if job is None or job.adapter_name != "sfm_only" or job.adapter_version != "2":
        return False
    try:
        return all(
            isinstance(job.published_outputs.get(key), str)
            and Path(str(job.published_outputs[key])).is_file()
            for key in (
                "trajectory",
                "solve_trajectory",
                "solve_video",
                "solve_frame_map",
                "core_frame_map",
            )
        )
    except OSError:
        return False


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _path_record_lock(path: Path) -> RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _RECORD_LOCKS.setdefault(key, RLock())
