from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
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
from .service import ProjectService


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
    save_permissions: tuple[str, ...]
    created_at: str
    expires_at: str
    return_to: str
    state: str
    workbench_output_revision: str | None = None
    workbench_output_fingerprint: str | None = None
    pending_output_revision: str | None = None
    pending_source_output_revision: str | None = None
    pending_source_output_fingerprint: str | None = None

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
            "save_permissions": list(self.save_permissions),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "return_to": self.return_to,
            "state": self.state,
            "workbench_output_revision": self.workbench_output_revision,
            "workbench_output_fingerprint": self.workbench_output_fingerprint,
            "pending_output_revision": self.pending_output_revision,
            "pending_source_output_revision": self.pending_source_output_revision,
            "pending_source_output_fingerprint": self.pending_source_output_fingerprint,
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
            pending_output_revision=_optional_string(
                value.get("pending_output_revision")
            ),
            pending_source_output_revision=_optional_string(
                value.get("pending_source_output_revision")
            ),
            pending_source_output_fingerprint=_optional_string(
                value.get("pending_source_output_fingerprint")
            ),
        )


class WorkbenchSessionStore(Protocol):
    def load(self, project_id: str, token: str) -> WorkbenchSession: ...

    def create(self, session: WorkbenchSession) -> WorkbenchSession: ...

    def load_by_token_hash(
        self, project_id: str, token_hash: str
    ) -> WorkbenchSession: ...

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
        if not (
            context.trajectory_output_revision
            and context.trajectory_output_fingerprint
            and context.trajectory_job_id
            and context.trajectory_run_id
        ):
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
        if self._expired(session):
            self.inspect(project_id, token)
            raise StaleWorkbenchSession("workbench session expired")
        context = self.resolve_context(project_id, session.clip_id)
        self._validate_binding(session, context)
        if "save" not in session.save_permissions or "save" not in context.save_permissions:
            raise WorkbenchPermissionDenied("workbench session has no save permission")
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
            if not revision:
                raise InvalidWorkbenchOutput("pending save has no immutable revision")
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
                ),
            )
        fingerprint = self._publish_output(
            pending,
            revision=revision,
            operation_id=operation_id,
            validated=validated,
        )
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
                pending_output_revision=None,
                pending_source_output_revision=None,
                pending_source_output_fingerprint=None,
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
                validated=validated,
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project_id": session.project_id,
            "clip_id": session.clip_id,
            "workflow": session.workflow,
            "workbench_output_revision": revision,
            "operation_id": operation_id,
            "created_at": _timestamp(self.now()),
            **dict(validated),
        }
        serialized = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        fingerprint = sha256(serialized).hexdigest()
        payload["workbench_output_fingerprint"] = fingerprint
        final_serialized = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
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
            manifest = temporary / "workbench_output_manifest.json"
            with manifest.open("wb") as stream:
                stream.write(final_serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                manifest = temporary / "workbench_output_manifest.json"
                manifest.unlink(missing_ok=True)
                temporary.rmdir()
        return fingerprint

    @staticmethod
    def _validate_existing_output(
        target: Path,
        *,
        session: WorkbenchSession,
        revision: str,
        operation_id: str,
        validated: Mapping[str, object],
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
            "source_output_revision": validated.get("source_output_revision"),
            "source_output_fingerprint": validated.get("source_output_fingerprint"),
        }
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
        return stored


class ProjectWorkbenchService:
    """Owns project/clip coordination around credential-only sessions."""

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
        can_open = current_job is not None and clip.resolved_workflow is not None
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
            save_permissions=(("save",) if can_open else ()),
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
            session = self.coordinator.create(
                project_id, clip_id, return_to=return_to
            )
            self._publish_clip_state(
                current,
                session,
                state="editing",
                expected_revision=expected_clips_revision,
            )
            return session

    def inspect(self, project_id: str, token: str) -> WorkbenchSession:
        return self.coordinator.inspect(project_id, token)

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
            session = self.coordinator.abandon(project_id, token)
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
        reference = next(
            (
                item
                for item in reversed(clip.references)
                if item.owner == "clips" and item.key == f"workbench:{clip.clip_id}"
            ),
            None,
        )
        state = (
            "ready"
            if self.resolve_context(project_id, clip.clip_id).can_open_workbench
            else "unavailable"
        )
        output_revision = None
        if reference is not None:
            state = str(reference.value.get("status") or state)
            output_revision = reference.value.get("workbench_output_revision")
            expires_at = reference.value.get("expires_at")
            if state == "editing" and isinstance(expires_at, str):
                if self.now() >= _parse_timestamp(expires_at):
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
        return self.resolve_context(project_id, clip_id).can_open_workbench

    def workbench_url(self, session: WorkbenchSession) -> str:
        from urllib.parse import urlencode

        return "/apps/web_camera_viewer/?" + urlencode(
            {
                "dataset": session.project_id,
                "runId": session.trajectory_run_id,
                "projectWorkbenchToken": session.token,
            }
        )

    @staticmethod
    def session_payload(session: WorkbenchSession) -> dict[str, object]:
        return {
            "token": session.token,
            "project_id": session.project_id,
            "clip_id": session.clip_id,
            "workflow": session.workflow,
            "trajectory_job_id": session.trajectory_job_id,
            "trajectory_run_id": session.trajectory_run_id,
            "state": session.state,
            "expires_at": session.expires_at,
            "return_to": session.return_to,
            "save_permissions": list(session.save_permissions),
            "session_revision": session.revision,
            "workbench_output_revision": session.workbench_output_revision,
        }

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
                        "expires_at": session.expires_at,
                        "workbench_output_revision": session.workbench_output_revision,
                        "workbench_output_fingerprint": session.workbench_output_fingerprint,
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
        expected = (
            self.viewer_runs_root
            / session.project_id
            / session.trajectory_run_id
            / "01_keyframes"
            / "camera_track_manual.json"
        ).resolve(strict=False)
        actual = Path(supplied).resolve(strict=False)
        try:
            expected.relative_to(self.viewer_runs_root)
        except ValueError as exc:
            raise InvalidWorkbenchOutput("path is not the bound workbench output") from exc
        if actual != expected:
            raise InvalidWorkbenchOutput("path is not the bound workbench output")
        if not actual.is_file():
            raise InvalidWorkbenchOutput("bound workbench output is missing")
        try:
            payload = json.loads(actual.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InvalidWorkbenchOutput(f"invalid bound workbench output: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("keyframes"), list):
            raise InvalidWorkbenchOutput("bound workbench output contract is invalid")
        fingerprint = sha256(actual.read_bytes()).hexdigest()
        return {
            "source_output_revision": f"manual-track:{fingerprint[:16]}",
            "source_output_fingerprint": fingerprint,
            "artifacts": {
                "camera_track": actual.relative_to(self.viewer_runs_root).as_posix()
            },
        }

    def _validate_pure_rotation_save(
        self, session: WorkbenchSession, receipt: Mapping[str, object]
    ) -> Mapping[str, object]:
        if receipt.get("kind") != "pure_rotation_calibration":
            raise InvalidWorkbenchOutput(
                "pure-rotation workbench save receipt is invalid"
            )
        run_root = (
            self.viewer_runs_root / session.project_id / session.trajectory_run_id
        ).resolve(strict=False)
        try:
            run_root.relative_to(self.viewer_runs_root)
        except ValueError as exc:
            raise InvalidWorkbenchOutput("pure-rotation run path escaped root") from exc
        candidates = (
            run_root
            / "04_pure_rotation_corrections"
            / "camera_track_corrected.json",
            run_root
            / "03_pure_rotation_placement"
            / "camera_track_cad_base.json",
        )
        actual = next((path for path in candidates if path.is_file()), None)
        if actual is None:
            raise InvalidWorkbenchOutput(
                "validated pure-rotation workbench output is missing"
            )
        try:
            payload = json.loads(actual.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
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
        fingerprint = sha256(actual.read_bytes()).hexdigest()
        return {
            "source_output_revision": f"pure-track:{fingerprint[:16]}",
            "source_output_fingerprint": fingerprint,
            "artifacts": {
                "camera_track": actual.relative_to(self.viewer_runs_root).as_posix()
            },
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


def _valid_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


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


def _path_record_lock(path: Path) -> RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _RECORD_LOCKS.setdefault(key, RLock())
