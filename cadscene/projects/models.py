from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Mapping, TypeVar, overload


SCHEMA_VERSION = "1.0"


def _copy_mapping(value: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return dict(value or {})


def _resolve_display_name(generated: str, custom: str | None) -> str:
    return generated if custom is None else custom


@dataclass(frozen=True)
class StateReference:
    """A non-owning pointer to state held by another manifest."""

    owner: str
    key: str
    operation_id: str
    value: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "key": self.key,
            "operation_id": self.operation_id,
            "value": dict(self.value),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StateReference:
        return cls(
            owner=str(value["owner"]),
            key=str(value["key"]),
            operation_id=str(value["operation_id"]),
            value=_copy_mapping(value.get("value")),
        )


@dataclass(frozen=True)
class OperationIntent:
    """Durable, non-recursive plan for an ordered manifest publication."""

    operation_id: str
    participants: tuple[str, ...]
    base_revisions: Mapping[str, int]
    candidates: Mapping[str, Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "participants": list(self.participants),
            "base_revisions": dict(self.base_revisions),
            "candidates": {
                owner: dict(candidate)
                for owner, candidate in self.candidates.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OperationIntent:
        return cls(
            operation_id=str(value["operation_id"]),
            participants=tuple(str(item) for item in value["participants"]),
            base_revisions={
                str(owner): int(revision)
                for owner, revision in value["base_revisions"].items()
            },
            candidates={
                str(owner): dict(candidate)
                for owner, candidate in value["candidates"].items()
            },
        )


@dataclass(frozen=True)
class ManifestHeader:
    schema_version: str
    revision: int
    updated_at: str
    project_id: str
    operation_id: str | None = None
    operation_intent: OperationIntent | None = None

    owner: ClassVar[str] = ""

    def __post_init__(self) -> None:
        if not self.schema_version:
            raise ValueError("schema_version must not be empty")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if not self.updated_at:
            raise ValueError("updated_at must not be empty")
        if not self.project_id:
            raise ValueError("project_id must not be empty")

    def _header_dict(self) -> dict[str, Any]:
        return {
            "manifest_owner": self.owner,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "operation_id": self.operation_id,
            "operation_intent": (
                None
                if self.operation_intent is None
                else self.operation_intent.to_dict()
            ),
        }


@dataclass(frozen=True)
class ClipWorkflow:
    recommended_workflow: str | None
    workflow_override: str | None
    resolved_workflow: str | None

    @classmethod
    def resolve(
        cls, recommended_workflow: str | None, workflow_override: str | None
    ) -> ClipWorkflow:
        return cls(
            recommended_workflow=recommended_workflow,
            workflow_override=workflow_override,
            resolved_workflow=(
                workflow_override
                if workflow_override is not None
                else recommended_workflow
            ),
        )


@dataclass(frozen=True)
class ClipDefinition:
    clip_id: str
    analysis_revision: str
    analysis: Mapping[str, Any]
    generated_display_name: str
    custom_display_name: str | None
    display_name: str
    recommended_workflow: str | None
    workflow_override: str | None
    resolved_workflow: str | None
    manual_definition: Mapping[str, Any] = field(default_factory=dict)
    references: tuple[StateReference, ...] = ()
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("clip_id must not be empty")
        if not self.analysis_revision:
            raise ValueError("analysis_revision must not be empty")
        expected_name = _resolve_display_name(
            self.generated_display_name, self.custom_display_name
        )
        if self.display_name != expected_name:
            raise ValueError("display_name must resolve the custom/generated name layers")
        expected_workflow = (
            self.workflow_override
            if self.workflow_override is not None
            else self.recommended_workflow
        )
        if self.resolved_workflow != expected_workflow:
            raise ValueError(
                "resolved_workflow must resolve the override/recommendation layers"
            )

    @classmethod
    def from_analysis(
        cls,
        logical_clip: Mapping[str, Any] | Any,
        *,
        generated_display_name: str | None = None,
        custom_display_name: str | None = None,
        workflow_override: str | None = None,
        manual_definition: Mapping[str, Any] | None = None,
        references: tuple[StateReference, ...] = (),
        operation_id: str | None = None,
    ) -> ClipDefinition:
        payload = (
            logical_clip.to_dict()
            if hasattr(logical_clip, "to_dict")
            else dict(logical_clip)
        )
        clip_id = str(payload["clip_id"])
        analysis_revision = str(payload["analysis_revision"])
        generated = generated_display_name or _generated_name(payload)
        recommendation = payload.get("recommended_workflow")
        if recommendation is not None:
            recommendation = str(recommendation)
        workflow = ClipWorkflow.resolve(recommendation, workflow_override)
        return cls(
            clip_id=clip_id,
            analysis_revision=analysis_revision,
            analysis=payload,
            generated_display_name=generated,
            custom_display_name=custom_display_name,
            display_name=_resolve_display_name(generated, custom_display_name),
            recommended_workflow=workflow.recommended_workflow,
            workflow_override=workflow.workflow_override,
            resolved_workflow=workflow.resolved_workflow,
            manual_definition=_copy_mapping(manual_definition),
            references=references,
            operation_id=operation_id,
        )

    def with_custom_display_name(self, value: str | None) -> ClipDefinition:
        return replace(
            self,
            custom_display_name=value,
            display_name=_resolve_display_name(self.generated_display_name, value),
        )

    def with_workflow_override(self, value: str | None) -> ClipDefinition:
        return replace(
            self,
            workflow_override=value,
            resolved_workflow=(
                value if value is not None else self.recommended_workflow
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "analysis_revision": self.analysis_revision,
            "analysis": dict(self.analysis),
            "generated_display_name": self.generated_display_name,
            "custom_display_name": self.custom_display_name,
            "display_name": self.display_name,
            "recommended_workflow": self.recommended_workflow,
            "workflow_override": self.workflow_override,
            "resolved_workflow": self.resolved_workflow,
            "manual_definition": dict(self.manual_definition),
            "references": [reference.to_dict() for reference in self.references],
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ClipDefinition:
        return cls(
            clip_id=str(value["clip_id"]),
            analysis_revision=str(value["analysis_revision"]),
            analysis=_copy_mapping(value.get("analysis")),
            generated_display_name=str(value["generated_display_name"]),
            custom_display_name=(
                None
                if value.get("custom_display_name") is None
                else str(value["custom_display_name"])
            ),
            display_name=str(value["display_name"]),
            recommended_workflow=(
                None
                if value.get("recommended_workflow") is None
                else str(value["recommended_workflow"])
            ),
            workflow_override=(
                None
                if value.get("workflow_override") is None
                else str(value["workflow_override"])
            ),
            resolved_workflow=(
                None
                if value.get("resolved_workflow") is None
                else str(value["resolved_workflow"])
            ),
            manual_definition=_copy_mapping(value.get("manual_definition")),
            references=tuple(
                StateReference.from_dict(item)
                for item in value.get("references", ())
            ),
            operation_id=(
                None if value.get("operation_id") is None else str(value["operation_id"])
            ),
        )


def _generated_name(payload: Mapping[str, Any]) -> str:
    scene = int(payload.get("scene_index", 1))
    segment = int(payload.get("segment_index", 1))
    return f"Scene {scene:02d} - Segment {segment}"


@dataclass(frozen=True)
class ProjectManifest(ManifestHeader):
    owner: ClassVar[str] = "project"

    source_assets: Mapping[str, Any] = field(default_factory=dict)
    media_spec_revision: str | None = None
    media_spec: Mapping[str, Any] | None = None
    project_state: str = "new"
    active_analysis_revision: str | None = None
    candidate_analysis_revision: str | None = None
    active_analysis_operation_id: str | None = None
    candidate_analysis_operation_id: str | None = None
    analysis_revisions: tuple[str, ...] = ()
    analysis_operation_ids: Mapping[str, str] = field(default_factory=dict)
    references: tuple[StateReference, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if (self.media_spec_revision is None) != (self.media_spec is None):
            raise ValueError("project media spec revision and value must be paired")
        if self.media_spec_revision is not None and (
            not self.media_spec_revision.strip() or not self.media_spec
        ):
            raise ValueError("project media spec revision and value must be explicit")
        if len(self.analysis_revisions) != len(set(self.analysis_revisions)):
            raise ValueError("analysis_revisions must be unique")
        if set(self.analysis_revisions) != set(self.analysis_operation_ids):
            raise ValueError(
                "analysis_operation_ids must exactly own every analysis revision"
            )
        if any(not operation_id for operation_id in self.analysis_operation_ids.values()):
            raise ValueError("analysis operation IDs must not be empty")
        for layer, revision, operation_id in (
            (
                "active",
                self.active_analysis_revision,
                self.active_analysis_operation_id,
            ),
            (
                "candidate",
                self.candidate_analysis_revision,
                self.candidate_analysis_operation_id,
            ),
        ):
            if (revision is None) != (operation_id is None):
                raise ValueError(
                    f"{layer} analysis revision and operation ID must be paired"
                )
            if revision is not None and revision not in self.analysis_operation_ids:
                raise ValueError(f"{layer} analysis revision must be immutable-owned")

    @classmethod
    def new(cls, project_id: str, *, updated_at: str) -> ProjectManifest:
        return cls(
            schema_version=SCHEMA_VERSION,
            revision=0,
            updated_at=updated_at,
            project_id=project_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._header_dict(),
            "source_assets": dict(self.source_assets),
            "media_spec_revision": self.media_spec_revision,
            "media_spec": None if self.media_spec is None else dict(self.media_spec),
            "project_state": self.project_state,
            "active_analysis_revision": self.active_analysis_revision,
            "candidate_analysis_revision": self.candidate_analysis_revision,
            "active_analysis_operation_id": self.active_analysis_operation_id,
            "candidate_analysis_operation_id": self.candidate_analysis_operation_id,
            "analysis_revisions": list(self.analysis_revisions),
            "analysis_operation_ids": dict(self.analysis_operation_ids),
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProjectManifest:
        return cls(
            **_header_from_dict(value, cls.owner),
            source_assets=_copy_mapping(value.get("source_assets")),
            media_spec_revision=_optional_str(value.get("media_spec_revision")),
            media_spec=(
                None
                if value.get("media_spec") is None
                else _copy_mapping(value.get("media_spec"))
            ),
            project_state=str(value.get("project_state", "new")),
            active_analysis_revision=_optional_str(
                value.get("active_analysis_revision")
            ),
            candidate_analysis_revision=_optional_str(
                value.get("candidate_analysis_revision")
            ),
            active_analysis_operation_id=_optional_str(
                value.get("active_analysis_operation_id")
            ),
            candidate_analysis_operation_id=_optional_str(
                value.get("candidate_analysis_operation_id")
            ),
            analysis_revisions=tuple(str(item) for item in value.get("analysis_revisions", ())),
            analysis_operation_ids={
                str(key): str(operation_id)
                for key, operation_id in value.get("analysis_operation_ids", {}).items()
            },
            references=_references_from_dict(value),
        )


@dataclass(frozen=True)
class ClipsManifest(ManifestHeader):
    owner: ClassVar[str] = "clips"

    analysis_revision: str | None = None
    clips: tuple[ClipDefinition, ...] = ()
    references: tuple[StateReference, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        clip_ids = [clip.clip_id for clip in self.clips]
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("clip_id values must be unique and permanent")
        if any(clip.analysis_revision != self.analysis_revision for clip in self.clips):
            raise ValueError("every clip must belong to the manifest analysis revision")

    @classmethod
    def new(
        cls,
        project_id: str,
        *,
        analysis_revision: str | None,
        clips: tuple[ClipDefinition, ...] = (),
        updated_at: str,
    ) -> ClipsManifest:
        return cls(
            schema_version=SCHEMA_VERSION,
            revision=0,
            updated_at=updated_at,
            project_id=project_id,
            analysis_revision=analysis_revision,
            clips=clips,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._header_dict(),
            "analysis_revision": self.analysis_revision,
            "clips": [clip.to_dict() for clip in self.clips],
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ClipsManifest:
        return cls(
            **_header_from_dict(value, cls.owner),
            analysis_revision=_optional_str(value.get("analysis_revision")),
            clips=tuple(ClipDefinition.from_dict(item) for item in value.get("clips", ())),
            references=_references_from_dict(value),
        )


@dataclass(frozen=True)
class JobsManifest(ManifestHeader):
    owner: ClassVar[str] = "jobs"

    jobs: tuple[Mapping[str, Any], ...] = ()
    queue_order: tuple[str, ...] = ()
    references: tuple[StateReference, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        job_ids = [job.get("job_id") for job in self.jobs]
        if any(not isinstance(job_id, str) or not job_id for job_id in job_ids):
            raise ValueError("every job state requires a non-empty job_id")
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("job_id values must be unique")

    @classmethod
    def new(cls, project_id: str, *, updated_at: str) -> JobsManifest:
        return cls(SCHEMA_VERSION, 0, updated_at, project_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._header_dict(),
            "jobs": [dict(job) for job in self.jobs],
            "queue_order": list(self.queue_order),
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobsManifest:
        return cls(
            **_header_from_dict(value, cls.owner),
            jobs=tuple(dict(item) for item in value.get("jobs", ())),
            queue_order=tuple(str(item) for item in value.get("queue_order", ())),
            references=_references_from_dict(value),
        )


@dataclass(frozen=True)
class RenderManifest(ManifestHeader):
    owner: ClassVar[str] = "render"

    clip_renders: tuple[Mapping[str, Any], ...] = ()
    merge_plans: tuple[Mapping[str, Any], ...] = ()
    published_outputs: tuple[Mapping[str, Any], ...] = ()
    references: tuple[StateReference, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        self._validate_ids(self.clip_renders, "render_id")
        self._validate_ids(self.merge_plans, "merge_id")
        self._validate_ids(self.published_outputs, "output_id")

    @staticmethod
    def _validate_ids(
        values: tuple[Mapping[str, Any], ...], identity_key: str
    ) -> None:
        identities = [value.get(identity_key) for value in values]
        if any(
            not isinstance(identity, str) or not identity
            for identity in identities
        ):
            raise ValueError(
                f"every render state requires a non-empty {identity_key}"
            )
        if len(identities) != len(set(identities)):
            raise ValueError(f"{identity_key} values must be unique")

    @classmethod
    def new(cls, project_id: str, *, updated_at: str) -> RenderManifest:
        return cls(SCHEMA_VERSION, 0, updated_at, project_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._header_dict(),
            "clip_renders": [dict(item) for item in self.clip_renders],
            "merge_plans": [dict(item) for item in self.merge_plans],
            "published_outputs": [dict(item) for item in self.published_outputs],
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RenderManifest:
        return cls(
            **_header_from_dict(value, cls.owner),
            clip_renders=tuple(dict(item) for item in value.get("clip_renders", ())),
            merge_plans=tuple(dict(item) for item in value.get("merge_plans", ())),
            published_outputs=tuple(
                dict(item) for item in value.get("published_outputs", ())
            ),
            references=_references_from_dict(value),
        )


def _header_from_dict(
    value: Mapping[str, Any], expected_owner: str
) -> dict[str, Any]:
    if value.get("manifest_owner") != expected_owner:
        raise ValueError(
            f"manifest_owner must be {expected_owner!r}, got "
            f"{value.get('manifest_owner')!r}"
        )
    return {
        "schema_version": str(value["schema_version"]),
        "revision": int(value["revision"]),
        "updated_at": str(value["updated_at"]),
        "project_id": str(value["project_id"]),
        "operation_id": _optional_str(value.get("operation_id")),
        "operation_intent": (
            None
            if value.get("operation_intent") is None
            else OperationIntent.from_dict(value["operation_intent"])
        ),
    }


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _references_from_dict(value: Mapping[str, Any]) -> tuple[StateReference, ...]:
    return tuple(StateReference.from_dict(item) for item in value.get("references", ()))


def _reconcile_clip(existing: ClipDefinition, candidate: ClipDefinition) -> ClipDefinition:
    if existing.clip_id != candidate.clip_id:
        raise ValueError("analysis refresh cannot change a permanent clip_id")
    workflow = ClipWorkflow.resolve(
        candidate.recommended_workflow, existing.workflow_override
    )
    return replace(
        candidate,
        custom_display_name=existing.custom_display_name,
        display_name=_resolve_display_name(
            candidate.generated_display_name, existing.custom_display_name
        ),
        workflow_override=workflow.workflow_override,
        resolved_workflow=workflow.resolved_workflow,
        manual_definition=dict(existing.manual_definition),
        references=existing.references,
    )


@overload
def reconcile_clips(
    *, existing: ClipDefinition, candidate: ClipDefinition
) -> ClipDefinition: ...


@overload
def reconcile_clips(
    *, existing: ClipsManifest, candidate: ClipsManifest
) -> ClipsManifest: ...


def reconcile_clips(
    *,
    existing: ClipDefinition | ClipsManifest,
    candidate: ClipDefinition | ClipsManifest,
) -> ClipDefinition | ClipsManifest:
    """Refresh analysis-owned values while retaining every user-owned layer."""

    if isinstance(existing, ClipDefinition) and isinstance(candidate, ClipDefinition):
        return _reconcile_clip(existing, candidate)
    if not isinstance(existing, ClipsManifest) or not isinstance(candidate, ClipsManifest):
        raise TypeError("existing and candidate must have the same manifest/clip type")
    if existing.project_id != candidate.project_id:
        raise ValueError("cannot reconcile clips from different projects")
    existing_by_id = {clip.clip_id: clip for clip in existing.clips}
    reconciled = tuple(
        _reconcile_clip(existing_by_id[clip.clip_id], clip)
        if clip.clip_id in existing_by_id
        else clip
        for clip in candidate.clips
    )
    return replace(candidate, clips=reconciled, references=existing.references)


def register_analysis_revision(
    project: ProjectManifest, analysis_revision: str, *, operation_id: str
) -> ProjectManifest:
    """Register immutable output; only the first analysis activates implicitly."""

    if not analysis_revision:
        raise ValueError("analysis_revision must not be empty")
    if not operation_id:
        raise ValueError("operation_id must not be empty")
    revisions = project.analysis_revisions
    operation_ids = dict(project.analysis_operation_ids)
    if analysis_revision in operation_ids:
        if operation_ids[analysis_revision] != operation_id:
            raise ValueError(
                "immutable analysis revision cannot be rebound to another operation"
            )
        return project
    if analysis_revision not in revisions:
        revisions = (*revisions, analysis_revision)
        operation_ids[analysis_revision] = operation_id
    if project.active_analysis_revision is None:
        return replace(
            project,
            active_analysis_revision=analysis_revision,
            candidate_analysis_revision=None,
            active_analysis_operation_id=operation_id,
            candidate_analysis_operation_id=None,
            analysis_revisions=revisions,
            analysis_operation_ids=operation_ids,
            operation_id=operation_id,
        )
    if analysis_revision == project.active_analysis_revision:
        raise ValueError("an active immutable analysis revision cannot be replaced")
    return replace(
        project,
        candidate_analysis_revision=analysis_revision,
        candidate_analysis_operation_id=operation_id,
        analysis_revisions=revisions,
        analysis_operation_ids=operation_ids,
        operation_id=operation_id,
    )


def activate_analysis_revision(
    project: ProjectManifest,
    existing_clips: ClipsManifest,
    candidate_clips: ClipsManifest,
    *,
    operation_id: str,
) -> tuple[ProjectManifest, ClipsManifest]:
    """Explicitly activate a candidate and carry forward user-owned clip layers."""

    candidate_revision = project.candidate_analysis_revision
    if candidate_revision is None:
        raise ValueError("project has no candidate analysis revision")
    if candidate_clips.analysis_revision != candidate_revision:
        raise ValueError("candidate clips do not match the project candidate revision")
    if (
        existing_clips.project_id != project.project_id
        or candidate_clips.project_id != project.project_id
    ):
        raise ValueError("analysis activation cannot cross project boundaries")
    reconciled = reconcile_clips(existing=existing_clips, candidate=candidate_clips)
    assert isinstance(reconciled, ClipsManifest)
    return (
        replace(
            project,
            active_analysis_revision=candidate_revision,
            candidate_analysis_revision=None,
            active_analysis_operation_id=operation_id,
            candidate_analysis_operation_id=None,
            operation_id=operation_id,
        ),
        replace(reconciled, operation_id=operation_id),
    )


Manifest = TypeVar(
    "Manifest", ProjectManifest, ClipsManifest, JobsManifest, RenderManifest
)
