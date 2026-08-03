from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from threading import RLock
from typing import Callable, Generic, Iterable, Protocol, TypeVar
from uuid import uuid4

from .models import ManifestHeader, stamp_new_operation_states


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
class ManifestMutation(Generic[T]):
    repository: ManifestRepository[T]
    project_id: str
    expected_revision: int
    mutate: Callable[[T], T]


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
        for repository in repositories:
            mutation = mutations_by_owner[repository.owner]
            current = repository.load(project_id)
            if current.revision != mutation.expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=mutation.expected_revision,
                    current_revision=current.revision,
                )
        for repository in repositories:
            mutation = mutations_by_owner[repository.owner]

            def mutate_and_stamp(
                value: ManifestHeader,
                mutate: Callable[[ManifestHeader], ManifestHeader] = mutation.mutate,
            ) -> ManifestHeader:
                return stamp_new_operation_states(mutate(value), operation_id)

            published.append(
                repository.update(
                    mutation.project_id,
                    expected_revision=mutation.expected_revision,
                    mutate=mutate_and_stamp,
                )
            )
    return CrossManifestResult(operation_id, tuple(published))
