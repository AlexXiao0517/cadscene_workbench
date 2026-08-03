from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Callable, Generic, Iterable, Mapping, Protocol, TypeVar
from uuid import uuid4

from .models import (
    ClipDefinition,
    ClipsManifest,
    JobsManifest,
    ManifestHeader,
    OperationIntent,
    RenderManifest,
    StateReference,
)


T = TypeVar("T", bound=ManifestHeader)


class RevisionConflict(RuntimeError):
    """A deterministic optimistic-concurrency failure."""

    code = "revision_conflict"

    def __init__(
        self,
        *,
        project_id: str,
        expected_revision: int,
        current_revision: int,
    ) -> None:
        self.project_id = project_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"revision_conflict for {project_id}: expected {expected_revision}, "
            f"current {current_revision}"
        )


class ManifestRepository(Protocol, Generic[T]):
    """Domain-facing persistence boundary; it contains no JSON concepts."""

    owner: str

    @property
    def process_lock(self) -> RLock: ...

    def lock_for(self, project_id: str) -> RLock: ...

    def load(self, project_id: str) -> T: ...

    def create(self, project_id: str, *, expected_revision: int, value: T) -> T: ...

    def update(
        self,
        project_id: str,
        *,
        expected_revision: int,
        mutate: Callable[[T], T],
    ) -> T: ...

@dataclass(frozen=True)
class PreparedManifest(Generic[T]):
    value: T
    payload: Mapping[str, Any]
    serialized: bytes


class _CoordinatedManifestRepository(ManifestRepository[T], Protocol[T]):
    """Internal primitives valid only while orchestration locks are held."""

    def _advance_candidate(
        self, project_id: str, *, expected_revision: int, value: T
    ) -> T: ...

    def _prepare_candidate(
        self, project_id: str, *, value: T
    ) -> PreparedManifest[T]: ...

    def _decode_candidate(
        self, project_id: str, value: Mapping[str, Any]
    ) -> T: ...

    def _publish_prepared_unchecked(
        self,
        project_id: str,
        *,
        expected_revision: int,
        prepared: PreparedManifest[T],
    ) -> T: ...


@dataclass(frozen=True)
class ManifestMutation(Generic[T]):
    repository: _CoordinatedManifestRepository[T]
    project_id: str
    expected_revision: int
    mutate: Callable[[T, str], T]


@dataclass(frozen=True)
class CrossManifestResult:
    operation_id: str
    manifests: tuple[ManifestHeader, ...]


_LOCK_ORDER = {"project": 0, "clips": 1, "jobs": 2, "render": 3}


def ordered_repositories(
    repositories: Iterable[ManifestRepository[ManifestHeader]],
) -> tuple[ManifestRepository[ManifestHeader], ...]:
    repositories = tuple(repositories)
    unknown = sorted(
        {repository.owner for repository in repositories} - set(_LOCK_ORDER)
    )
    if unknown:
        raise ValueError(f"unknown manifest owners: {unknown}")
    return tuple(sorted(repositories, key=lambda item: _LOCK_ORDER[item.owner]))


def new_operation_id() -> str:
    return uuid4().hex


def _reference_identity(reference: StateReference) -> tuple[str, str]:
    return reference.owner, reference.key


def _reference_content(reference: StateReference) -> dict[str, Any]:
    content = reference.to_dict()
    content.pop("operation_id", None)
    return content


def _stamp_references(
    current: tuple[StateReference, ...],
    candidate: tuple[StateReference, ...],
    operation_id: str,
) -> tuple[StateReference, ...]:
    current_by_identity = {_reference_identity(item): item for item in current}
    stamped: list[StateReference] = []
    for reference in candidate:
        previous = current_by_identity.get(_reference_identity(reference))
        if previous is not None and _reference_content(previous) == _reference_content(
            reference
        ):
            explicit_operation_id = (
                reference.operation_id
                if reference.operation_id
                and reference.operation_id != previous.operation_id
                else previous.operation_id
            )
            stamped.append(replace(reference, operation_id=explicit_operation_id))
        else:
            stamped.append(replace(reference, operation_id=operation_id))
    return tuple(stamped)


def _clip_content(clip: ClipDefinition) -> dict[str, Any]:
    content = clip.to_dict()
    content.pop("operation_id", None)
    for reference in content["references"]:
        reference.pop("operation_id", None)
    return content


def _mapping_content(value: Mapping[str, Any]) -> dict[str, Any]:
    content = dict(value)
    content.pop("operation_id", None)
    return content


def _stamp_mapping_items(
    current: tuple[Mapping[str, Any], ...],
    candidate: tuple[Mapping[str, Any], ...],
    operation_id: str,
    identity_keys: tuple[str, ...],
) -> tuple[Mapping[str, Any], ...]:
    def identity(value: Mapping[str, Any]) -> tuple[str, str]:
        for key in identity_keys:
            if value.get(key) is not None:
                return key, str(value[key])
        raise ValueError("nested state is missing a stable identity")

    current_by_identity = {identity(value): value for value in current}
    stamped: list[Mapping[str, Any]] = []
    for value in candidate:
        previous = current_by_identity.get(identity(value))
        if previous is not None and _mapping_content(previous) == _mapping_content(
            value
        ):
            stamped.append(
                {**value, "operation_id": previous.get("operation_id")}
            )
        else:
            stamped.append({**value, "operation_id": operation_id})
    return tuple(stamped)


def stamp_operation_changes(
    current: ManifestHeader,
    candidate: ManifestHeader,
    operation_id: str,
) -> ManifestHeader:
    """Stamp changed/new owned state while retaining unchanged history markers."""

    stamped: ManifestHeader = replace(
        candidate,
        operation_id=operation_id,
        operation_intent=None,
        references=_stamp_references(
            current.references, candidate.references, operation_id
        ),
    )
    if isinstance(current, ClipsManifest) and isinstance(candidate, ClipsManifest):
        current_by_id = {clip.clip_id: clip for clip in current.clips}
        clips: list[ClipDefinition] = []
        for clip in candidate.clips:
            previous = current_by_id.get(clip.clip_id)
            references = _stamp_references(
                () if previous is None else previous.references,
                clip.references,
                operation_id,
            )
            clip_operation_id = (
                previous.operation_id
                if previous is not None
                and _clip_content(previous) == _clip_content(clip)
                else operation_id
            )
            clips.append(
                replace(
                    clip,
                    operation_id=clip_operation_id,
                    references=references,
                )
            )
        stamped = replace(stamped, clips=tuple(clips))
    elif isinstance(current, JobsManifest) and isinstance(candidate, JobsManifest):
        stamped = replace(
            stamped,
            jobs=_stamp_mapping_items(
                current.jobs, candidate.jobs, operation_id, ("job_id",)
            ),
        )
    elif isinstance(current, RenderManifest) and isinstance(candidate, RenderManifest):
        stamped = replace(
            stamped,
            clip_renders=_stamp_mapping_items(
                current.clip_renders,
                candidate.clip_renders,
                operation_id,
                ("render_id",),
            ),
            merge_plans=_stamp_mapping_items(
                current.merge_plans,
                candidate.merge_plans,
                operation_id,
                ("merge_id",),
            ),
            published_outputs=_stamp_mapping_items(
                current.published_outputs,
                candidate.published_outputs,
                operation_id,
                ("output_id",),
            ),
        )
    return stamped


def publish_manifests(
    mutations: Iterable[ManifestMutation[ManifestHeader]],
) -> CrossManifestResult:
    """Publish ordered atomic files carrying one recovery marker.

    Each file replacement is atomic, but the sequence intentionally is not a
    transaction. A crash can leave a prefix published for recovery to inspect.
    """

    mutations = tuple(mutations)
    if not mutations:
        raise ValueError("a cross-manifest publication needs at least one mutation")
    project_ids = {mutation.project_id for mutation in mutations}
    if len(project_ids) != 1:
        raise ValueError("one publication cannot span project ids")
    project_id = next(iter(project_ids))
    owners = [mutation.repository.owner for mutation in mutations]
    if len(owners) != len(set(owners)):
        raise ValueError("one publication can mutate each manifest at most once")

    operation_id = new_operation_id()
    mutations_by_owner = {
        mutation.repository.owner: mutation for mutation in mutations
    }
    repositories = ordered_repositories(
        mutation.repository for mutation in mutations
    )
    published: list[ManifestHeader] = []
    with ExitStack() as stack:
        for repository in repositories:
            stack.enter_context(repository.lock_for(project_id))
        current_by_owner: dict[str, ManifestHeader] = {}
        for repository in repositories:
            mutation = mutations_by_owner[repository.owner]
            current = repository.load(project_id)
            if current.revision != mutation.expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=mutation.expected_revision,
                    current_revision=current.revision,
                )
            current_by_owner[repository.owner] = current

        advanced_by_owner: dict[str, ManifestHeader] = {}
        for repository in repositories:
            mutation = mutations_by_owner[repository.owner]
            current = current_by_owner[repository.owner]
            candidate = mutation.mutate(current, operation_id)
            stamped = stamp_operation_changes(current, candidate, operation_id)
            advanced_by_owner[repository.owner] = repository._advance_candidate(
                project_id,
                expected_revision=mutation.expected_revision,
                value=stamped,
            )

        snapshots = {
            repository.owner: repository._prepare_candidate(
                project_id,
                value=advanced_by_owner[repository.owner],
            )
            for repository in repositories
        }

        intent = OperationIntent(
            operation_id=operation_id,
            participants=tuple(repository.owner for repository in repositories),
            base_revisions={
                owner: current.revision
                for owner, current in current_by_owner.items()
            },
            candidates={
                owner: dict(snapshot.payload)
                for owner, snapshot in snapshots.items()
            },
        )
        final_by_owner = {
            owner: replace(advanced, operation_intent=intent)
            for owner, advanced in advanced_by_owner.items()
        }
        final_prepared = {
            repository.owner: repository._prepare_candidate(
                project_id,
                value=final_by_owner[repository.owner],
            )
            for repository in repositories
        }
        for repository in repositories:
            mutation = mutations_by_owner[repository.owner]
            published.append(
                repository._publish_prepared_unchecked(
                    project_id,
                    expected_revision=mutation.expected_revision,
                    prepared=final_prepared[repository.owner],
                )
            )
    return CrossManifestResult(operation_id, tuple(published))
