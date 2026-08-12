from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Mapping

from .json_repositories import ProjectRepositories
from .models import (
    ClipDefinition,
    ClipsManifest,
    JobsManifest,
    ManifestHeader,
    OperationIntent,
    ProjectManifest,
    RenderManifest,
    StateReference,
)
from .repositories import (
    ManifestMutation,
    _publish_recovery_restoration,
    ordered_repositories,
    publish_manifests,
    stamp_operation_changes,
    validate_manifest_transition,
)


@dataclass(frozen=True)
class ReconciliationResult:
    project_id: str
    operation_id: str | None
    completed_references: int
    rolled_back_references: int
    changed_owners: tuple[str, ...]


def _owned_states(manifest: ManifestHeader) -> dict[str, str]:
    states: dict[str, str] = {}
    if isinstance(manifest, ProjectManifest):
        for revision, operation_id in manifest.analysis_operation_ids.items():
            states[f"analysis:{revision}"] = operation_id
        if (
            manifest.active_analysis_revision
            and manifest.active_analysis_operation_id
        ):
            states.setdefault(
                f"analysis:{manifest.active_analysis_revision}",
                manifest.active_analysis_operation_id
            )
            states["active_analysis"] = manifest.active_analysis_operation_id
        if (
            manifest.candidate_analysis_revision
            and manifest.candidate_analysis_operation_id
        ):
            states.setdefault(
                f"analysis:{manifest.candidate_analysis_revision}",
                manifest.candidate_analysis_operation_id
            )
            states["candidate_analysis"] = (
                manifest.candidate_analysis_operation_id
            )
    elif isinstance(manifest, ClipsManifest):
        for clip in manifest.clips:
            operation_id = clip.operation_id or manifest.operation_id
            if operation_id:
                states[f"clip:{clip.clip_id}"] = operation_id
            for reference in clip.references:
                if (
                    reference.owner == "clips"
                    and reference.key == f"workbench:{clip.clip_id}"
                    and reference.value.get("status")
                    in {"editing", "pending_save", "saved"}
                    and reference.value.get("workbench_output_revision")
                    and reference.value.get("workbench_output_fingerprint")
                ):
                    states[reference.key] = reference.operation_id
    elif isinstance(manifest, JobsManifest):
        for job in manifest.jobs:
            if job.get("job_id") and job.get("operation_id"):
                states[f"job:{job['job_id']}"] = str(job["operation_id"])
    elif isinstance(manifest, RenderManifest):
        for items, identity_key, reference_prefix in (
            (manifest.clip_renders, "render_id", "clip_render"),
            (manifest.merge_plans, "merge_id", "merge"),
            (manifest.published_outputs, "output_id", "output"),
        ):
            for item in items:
                identifier = item.get(identity_key)
                if identifier and item.get("operation_id"):
                    states[f"{reference_prefix}:{identifier}"] = str(
                        item["operation_id"]
                    )
    return states


def _recover_operation_prefix(
    project_id: str,
    repositories: ProjectRepositories,
    manifests: tuple[ManifestHeader, ...],
) -> tuple[tuple[str, ...], str | None]:
    repository_by_owner = {
        repository.owner: repository for repository in repositories.in_lock_order()
    }
    manifest_by_owner = {manifest.owner: manifest for manifest in manifests}
    intents: dict[str, OperationIntent] = {}
    for manifest in manifests:
        intent = manifest.operation_intent
        if intent is None:
            continue
        previous = intents.get(intent.operation_id)
        if previous is not None and previous != intent:
            raise ValueError("operation intent differs across participant manifests")
        intents[intent.operation_id] = intent

    changed: list[str] = []
    recovered_operation_id: str | None = None
    for intent in intents.values():
        participants = intent.participants
        if not intent.operation_id:
            raise ValueError("operation intent ID must not be empty")
        if len(participants) != len(set(participants)):
            raise ValueError("operation intent participants must be unique")
        try:
            participant_repositories = tuple(
                repository_by_owner[owner] for owner in participants
            )
        except KeyError as exc:
            raise ValueError("operation intent contains an unknown owner") from exc
        canonical_participants = tuple(
            repository.owner
            for repository in ordered_repositories(participant_repositories)
        )
        if participants != canonical_participants:
            raise ValueError("operation intent participants are not in lock order")
        if set(participants) != set(intent.base_revisions) or set(participants) != set(
            intent.candidates
        ):
            raise ValueError("operation intent participant metadata is incomplete")
        if any(revision < 0 for revision in intent.base_revisions.values()):
            raise ValueError("operation intent base revisions must be non-negative")
        candidates = {
            owner: repository_by_owner[owner]._decode_candidate(
                project_id,
                intent.candidates[owner]
            )
            for owner in participants
        }
        for owner, candidate in candidates.items():
            if candidate.operation_id != intent.operation_id:
                raise ValueError("candidate operation ID does not match its intent")
            if candidate.operation_intent is not None:
                raise ValueError("intent candidate payload must be non-recursive")
            if candidate.revision != intent.base_revisions[owner] + 1:
                raise ValueError("intent candidate revision does not advance once")
        published: list[str] = []
        pending: list[str] = []
        superseded = False
        for owner in participants:
            current = manifest_by_owner[owner]
            candidate = candidates[owner]
            if (
                current.revision == candidate.revision
                and current.operation_id == intent.operation_id
            ):
                if replace(current, operation_intent=None) != candidate:
                    raise ValueError(
                        "published manifest differs from its intent candidate"
                    )
                published.append(owner)
            elif current.revision == intent.base_revisions[owner]:
                restamped = stamp_operation_changes(
                    current, candidate, intent.operation_id
                )
                if restamped != candidate:
                    raise ValueError(
                        "candidate nested operation state does not match its intent"
                    )
                validate_manifest_transition(
                    current,
                    candidate,
                    publication_operation_id=intent.operation_id,
                )
                pending.append(owner)
            elif current.revision > candidate.revision:
                superseded = True
            else:
                raise ValueError(
                    f"cannot reconcile operation {intent.operation_id} owner {owner}"
                )
        if superseded:
            continue
        if not published or not pending:
            continue
        prepared_pending = {
            owner: repository_by_owner[owner]._prepare_intent_candidate(
                project_id,
                value=candidates[owner],
                payload=intent.candidates[owner],
                intent=intent,
            )
            for owner in pending
        }
        for owner in participants:
            if owner not in pending:
                continue
            repository = repository_by_owner[owner]
            repository._publish_prepared_unchecked(
                project_id,
                expected_revision=intent.base_revisions[owner],
                prepared=prepared_pending[owner],
            )
            manifest_by_owner[owner] = prepared_pending[owner].value
            changed.append(owner)
        recovered_operation_id = intent.operation_id
    return tuple(changed), recovered_operation_id


def _repair_reference_tuple(
    references: tuple[StateReference, ...],
    owned: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[StateReference, ...], int, int]:
    repaired: list[StateReference] = []
    completed = 0
    rolled_back = 0
    for reference in references:
        owner_operation = owned.get(reference.owner, {}).get(reference.key)
        if owner_operation is None:
            rolled_back += 1
            continue
        if owner_operation != reference.operation_id:
            reference = replace(reference, operation_id=owner_operation)
            completed += 1
        repaired.append(reference)
    return tuple(repaired), completed, rolled_back


def _repair_manifest(
    manifest: ManifestHeader,
    owned: Mapping[str, Mapping[str, str]],
) -> tuple[ManifestHeader, int, int]:
    references, completed, rolled_back = _repair_reference_tuple(
        manifest.references, owned
    )
    repaired: ManifestHeader = replace(manifest, references=references)
    if isinstance(manifest, ClipsManifest):
        clips: list[ClipDefinition] = []
        for clip in manifest.clips:
            clip_references, clip_completed, clip_rolled_back = _repair_reference_tuple(
                clip.references, owned
            )
            completed += clip_completed
            rolled_back += clip_rolled_back
            clips.append(replace(clip, references=clip_references))
        repaired = replace(repaired, clips=tuple(clips))
    return repaired, completed, rolled_back


def reconcile_project(
    project_id: str,
    *,
    repositories: ProjectRepositories,
) -> ReconciliationResult:
    """Repair cross-manifest references according to their state owner.

    A present owner state completes a stale reference to the authoritative
    operation ID. A reference whose owner state is absent is rolled back. The
    repair itself is another ordered, recoverable publication, not a transaction.
    """

    creation_changed, creation_operation_id = repositories.recover_partial_creation(
        project_id
    )
    repository_order = repositories.in_lock_order()
    with ExitStack() as stack:
        for repository in repository_order:
            stack.enter_context(repository.lock_for(project_id))
        original_manifests = tuple(
            repository.load(project_id) for repository in repository_order
        )
        intent_changed, intent_operation_id = _recover_operation_prefix(
            project_id, repositories, original_manifests
        )
        if intent_changed:
            original_manifests = tuple(
                repository.load(project_id) for repository in repository_order
            )
        manifests = original_manifests
        project = manifests[0]
        clips = manifests[1]
        assert isinstance(project, ProjectManifest)
        assert isinstance(clips, ClipsManifest)
        restore_analysis_pointers = False
        if project.active_analysis_revision != clips.analysis_revision:
            restore_analysis_pointers = True
            failed_activation = project.active_analysis_revision
            project = replace(
                project,
                active_analysis_revision=clips.analysis_revision,
                active_analysis_operation_id=(
                    project.analysis_operation_ids.get(clips.analysis_revision)
                    if clips.analysis_revision is not None
                    else None
                ),
                candidate_analysis_revision=failed_activation,
                candidate_analysis_operation_id=project.active_analysis_operation_id,
            )
            manifests = (project, *manifests[1:])
        owned = {
            manifest.owner: _owned_states(manifest) for manifest in manifests
        }
        repairs: list[tuple[object, ManifestHeader]] = []
        completed = 0
        rolled_back = 0
        for repository, manifest, original in zip(
            repository_order, manifests, original_manifests
        ):
            repaired, manifest_completed, manifest_rolled_back = _repair_manifest(
                manifest, owned
            )
            completed += manifest_completed
            rolled_back += manifest_rolled_back
            if repaired != original:
                repairs.append((repository, repaired))

        if not repairs:
            return ReconciliationResult(
                project_id,
                intent_operation_id or creation_operation_id,
                0,
                0,
                (*creation_changed, *intent_changed),
            )

        mutations = tuple(
            ManifestMutation(
                repository,
                project_id,
                repaired.revision,
                lambda _current, _operation_id, repaired=repaired: repaired,
            )
            for repository, repaired in repairs
        )
        result = (
            _publish_recovery_restoration(
                mutations,
                restored_analysis_revision=clips.analysis_revision,
            )
            if restore_analysis_pointers
            else publish_manifests(mutations)
        )
        return ReconciliationResult(
            project_id=project_id,
            operation_id=result.operation_id,
            completed_references=completed,
            rolled_back_references=rolled_back,
            changed_owners=(
                *creation_changed,
                *intent_changed,
                *(manifest.owner for manifest in result.manifests),
            ),
        )
