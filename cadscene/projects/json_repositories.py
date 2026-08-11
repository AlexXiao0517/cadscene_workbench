from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Lock, RLock
from typing import Any, Callable, Generic, Mapping, TypeVar

from .models import (
    ClipsManifest,
    JobsManifest,
    ManifestHeader,
    OperationIntent,
    ProjectManifest,
    RenderManifest,
)
from .repositories import (
    PreparedManifest,
    RevisionConflict,
    new_operation_id,
    stamp_operation_changes,
    validate_manifest_transition,
)
from .identifiers import validate_project_id


T = TypeVar("T", bound=ManifestHeader)
PathSource = Path | Callable[[str], Path]


_LOCKS_GUARD = Lock()
_PATH_LOCKS: dict[str, RLock] = {}


def _path_lock(path: Path) -> RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, RLock())


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AtomicJsonRepository(Generic[T]):
    """Atomic JSON implementation of the domain repository protocol."""

    def __init__(
        self,
        path: PathSource,
        *,
        owner: str,
        decoder: Callable[[Mapping[str, Any]], T],
        encoder: Callable[[T], Mapping[str, Any]] | None = None,
    ) -> None:
        self._path_source = path
        self.owner = owner
        self._decoder = decoder
        self._encoder = encoder or (lambda value: value.to_dict())
        # Fixed paths can expose their shared lock without a project lookup.
        self._fixed_lock = _path_lock(path) if isinstance(path, Path) else None

    def path_for(self, project_id: str) -> Path:
        project_id = validate_project_id(project_id)
        source = self._path_source
        return source(project_id) if callable(source) else source

    @property
    def process_lock(self) -> RLock:
        if self._fixed_lock is None:
            raise RuntimeError(
                "project-dependent repository locks require lock_for(project_id)"
            )
        return self._fixed_lock

    def lock_for(self, project_id: str) -> RLock:
        return _path_lock(self.path_for(project_id))

    def load(self, project_id: str) -> T:
        with self.lock_for(project_id):
            return self._read_unlocked(project_id)

    def create(
        self, project_id: str, *, expected_revision: int, value: T
    ) -> T:
        path = self.path_for(project_id)
        with self.lock_for(project_id):
            if path.exists():
                current = self._read_unlocked(project_id)
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            if expected_revision != -1:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=-1,
                )
            self._validate_identity(project_id, value)
            if value.revision != 0:
                raise ValueError("a newly created manifest must start at revision 0")
            prepared = self._atomic_write(value)
            return prepared.value

    def update(
        self,
        project_id: str,
        *,
        expected_revision: int,
        mutate: Callable[[T], T],
    ) -> T:
        with self.lock_for(project_id):
            current = self._read_unlocked(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            candidate = mutate(deepcopy(current))
            candidate = replace(candidate, operation_intent=None)
            publication_operation_id = candidate.operation_id
            if isinstance(current, ProjectManifest) and isinstance(
                candidate, ProjectManifest
            ):
                if (
                    publication_operation_id is None
                    or publication_operation_id == current.operation_id
                ):
                    publication_operation_id = new_operation_id()
                candidate = stamp_operation_changes(
                    current,
                    candidate,
                    publication_operation_id,
                )
            validate_manifest_transition(
                current,
                candidate,
                publication_operation_id=publication_operation_id,
            )
            advanced = self._advance_candidate(
                project_id,
                expected_revision=expected_revision,
                value=candidate,
            )
            prepared = self._prepare_candidate(project_id, value=advanced)
            return self._publish_prepared_unchecked(
                project_id,
                expected_revision=expected_revision,
                prepared=prepared,
            )

    def _advance_candidate(
        self, project_id: str, *, expected_revision: int, value: T
    ) -> T:
        self._validate_identity(project_id, value)
        if value.revision != expected_revision:
            raise ValueError("mutators must not manage repository revisions")
        return replace(
            value,
            revision=expected_revision + 1,
            updated_at=_utc_now(),
        )

    def _prepare_candidate(
        self, project_id: str, *, value: T
    ) -> PreparedManifest[T]:
        self._validate_identity(project_id, value)
        payload = self._encoder(value)
        serialized = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        normalized = json.loads(serialized)
        decoded = self._decoder(normalized)
        self._validate_identity(project_id, decoded)
        if decoded != value:
            raise ValueError("manifest encoder must round-trip semantically exactly")
        return PreparedManifest(
            value=decoded,
            payload=normalized,
            serialized=serialized.encode("utf-8"),
        )

    def _prepare_intent_candidate(
        self,
        project_id: str,
        *,
        value: T,
        payload: Mapping[str, Any],
        intent: OperationIntent,
    ) -> PreparedManifest[T]:
        if value.operation_intent is not None:
            raise ValueError("intent candidate value must be non-recursive")
        final_payload = dict(payload)
        final_payload["operation_intent"] = intent.to_dict()
        serialized = (
            json.dumps(
                final_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        normalized = json.loads(serialized)
        decoded = self._decoder(normalized)
        self._validate_identity(project_id, decoded)
        expected = replace(value, operation_intent=intent)
        if decoded != expected:
            raise ValueError(
                "normalized manifest plus intent must round-trip semantically exactly"
            )
        return PreparedManifest(
            value=decoded,
            payload=normalized,
            serialized=serialized.encode("utf-8"),
        )

    def _decode_candidate(
        self, project_id: str, value: Mapping[str, Any]
    ) -> T:
        candidate = self._decoder(value)
        self._validate_identity(project_id, candidate)
        return candidate

    def _publish_prepared_unchecked(
        self,
        project_id: str,
        *,
        expected_revision: int,
        prepared: PreparedManifest[T],
    ) -> T:
        with self.lock_for(project_id):
            value = prepared.value
            self._validate_identity(project_id, value)
            if value.revision != expected_revision + 1:
                raise ValueError("prepared manifest revision must advance exactly once")
            self._atomic_write_bytes(
                self.path_for(project_id), prepared.serialized
            )
            return value

    def _validate_identity(self, project_id: str, value: T) -> None:
        if value.project_id != project_id:
            raise ValueError("manifest project_id does not match repository key")
        if value.owner != self.owner:
            raise ValueError(
                f"{self.owner} repository cannot store {value.owner} manifest"
            )

    def _read_unlocked(self, project_id: str) -> T:
        path = self.path_for(project_id)
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        value = self._decoder(payload)
        self._validate_identity(project_id, value)
        return value

    def _atomic_write(self, value: T) -> PreparedManifest[T]:
        prepared = self._prepare_candidate(value.project_id, value=value)
        self._atomic_write_bytes(
            self.path_for(value.project_id), prepared.serialized
        )
        return prepared

    def _atomic_write_bytes(self, path: Path, serialized: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProjectRepositories:
    project: AtomicJsonRepository[ProjectManifest]
    clips: AtomicJsonRepository[ClipsManifest]
    jobs: AtomicJsonRepository[JobsManifest]
    render: AtomicJsonRepository[RenderManifest]

    def in_lock_order(self) -> tuple[AtomicJsonRepository[Any], ...]:
        return (self.project, self.clips, self.jobs, self.render)

    def create_project(self, project_id: str, *, updated_at: str) -> None:
        project_id = validate_project_id(project_id)
        operation_id = new_operation_id()
        values: tuple[ManifestHeader, ...] = (
            replace(
                ProjectManifest.new(project_id, updated_at=updated_at),
                operation_id=operation_id,
            ),
            replace(
                ClipsManifest.new(
                    project_id,
                    analysis_revision=None,
                    updated_at=updated_at,
                ),
                operation_id=operation_id,
            ),
            replace(
                JobsManifest.new(project_id, updated_at=updated_at),
                operation_id=operation_id,
            ),
            replace(
                RenderManifest.new(project_id, updated_at=updated_at),
                operation_id=operation_id,
            ),
        )
        repositories = self.in_lock_order()
        with ExitStack() as stack:
            for repository in repositories:
                stack.enter_context(repository.lock_for(project_id))
            for repository, value in zip(repositories, values):
                repository.create(project_id, expected_revision=-1, value=value)

    def recover_partial_creation(
        self, project_id: str
    ) -> tuple[tuple[str, ...], str | None]:
        repositories = self.in_lock_order()
        with ExitStack() as stack:
            for repository in repositories:
                stack.enter_context(repository.lock_for(project_id))
            exists = tuple(
                repository.path_for(project_id).is_file()
                for repository in repositories
            )
            if all(exists):
                return (), None
            if not any(exists):
                raise FileNotFoundError(f"project manifests do not exist: {project_id}")
            published_count = next(
                (index for index, present in enumerate(exists) if not present),
                len(exists),
            )
            if any(exists[published_count:]):
                raise ValueError("partial project creation is not an ordered prefix")
            existing = tuple(
                repository.load(project_id)
                for repository in repositories[:published_count]
            )
            operation_ids = {manifest.operation_id for manifest in existing}
            if None in operation_ids or len(operation_ids) != 1:
                raise ValueError("partial project creation has inconsistent operation IDs")
            if any(manifest.revision != 0 for manifest in existing):
                raise ValueError("partial project creation contains mutated manifests")
            operation_id = next(iter(operation_ids))
            updated_at = existing[0].updated_at
            values: tuple[ManifestHeader, ...] = (
                replace(
                    ProjectManifest.new(project_id, updated_at=updated_at),
                    operation_id=operation_id,
                ),
                replace(
                    ClipsManifest.new(
                        project_id,
                        analysis_revision=None,
                        updated_at=updated_at,
                    ),
                    operation_id=operation_id,
                ),
                replace(
                    JobsManifest.new(project_id, updated_at=updated_at),
                    operation_id=operation_id,
                ),
                replace(
                    RenderManifest.new(project_id, updated_at=updated_at),
                    operation_id=operation_id,
                ),
            )
            changed: list[str] = []
            for repository, value in zip(
                repositories[published_count:], values[published_count:]
            ):
                repository.create(project_id, expected_revision=-1, value=value)
                changed.append(repository.owner)
            return tuple(changed), operation_id


def project_repositories(root: Path) -> ProjectRepositories:
    def manifest_path(name: str) -> Callable[[str], Path]:
        return lambda project_id: root / validate_project_id(project_id) / name

    return ProjectRepositories(
        project=AtomicJsonRepository(
            manifest_path("project_manifest.json"),
            owner="project",
            decoder=ProjectManifest.from_dict,
        ),
        clips=AtomicJsonRepository(
            manifest_path("clips_manifest.json"),
            owner="clips",
            decoder=ClipsManifest.from_dict,
        ),
        jobs=AtomicJsonRepository(
            manifest_path("jobs_manifest.json"),
            owner="jobs",
            decoder=JobsManifest.from_dict,
        ),
        render=AtomicJsonRepository(
            manifest_path("render_manifest.json"),
            owner="render",
            decoder=RenderManifest.from_dict,
        ),
    )
