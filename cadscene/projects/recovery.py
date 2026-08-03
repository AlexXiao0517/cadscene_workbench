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
    ProjectManifest,
    RenderManifest,
    StateReference,
)
from .repositories import ManifestMutation, publish_manifests


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
        if (
            manifest.active_analysis_revision
            and manifest.active_analysis_operation_id
        ):
            states[f"analysis:{manifest.active_analysis_revision}"] = (
                manifest.active_analysis_operation_id
            )
        if (
            manifest.candidate_analysis_revision
            and manifest.candidate_analysis_operation_id
        ):
            states[f"analysis:{manifest.candidate_analysis_revision}"] = (
                manifest.candidate_analysis_operation_id
            )
    elif isinstance(manifest, ClipsManifest):
        for clip in manifest.clips:
            operation_id = clip.operation_id or manifest.operation_id
            if operation_id:
                states[f"clip:{clip.clip_id}"] = operation_id
    elif isinstance(manifest, JobsManifest):
        for job in manifest.jobs:
            if job.get("job_id") and job.get("operation_id"):
                states[f"job:{job['job_id']}"] = str(job["operation_id"])
    elif isinstance(manifest, RenderManifest):
        for item in (
            *manifest.clip_renders,
            *manifest.merge_plans,
            *manifest.published_outputs,
        ):
            identifier = (
                item.get("render_id")
                or item.get("merge_id")
                or item.get("output_id")
                or item.get("clip_id")
            )
            if identifier and item.get("operation_id"):
                states[f"render:{identifier}"] = str(item["operation_id"])
    return states


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
        manifests = original_manifests
        project = manifests[0]
        clips = manifests[1]
        assert isinstance(project, ProjectManifest)
        assert isinstance(clips, ClipsManifest)
        if project.active_analysis_revision != clips.analysis_revision:
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
                creation_operation_id,
                0,
                0,
                creation_changed,
            )

        result = publish_manifests(
            ManifestMutation(
                repository,
                project_id,
                repaired.revision,
                lambda _current, repaired=repaired: repaired,
            )
            for repository, repaired in repairs
        )
        return ReconciliationResult(
            project_id=project_id,
            operation_id=result.operation_id,
            completed_references=completed,
            rolled_back_references=rolled_back,
            changed_owners=(
                *creation_changed,
                *(manifest.owner for manifest in result.manifests),
            ),
        )
