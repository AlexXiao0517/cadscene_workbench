from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable, Mapping

from cadscene.projects.json_repositories import ProjectRepositories
from cadscene.projects.models import ClipDefinition, RenderManifest
from cadscene.projects.repositories import (
    ManifestMutation,
    RevisionConflict,
    publish_manifests,
)

from .models import (
    Annotation,
    AnnotationContent,
    AnnotationLeader,
    AnnotationPanel,
    AnnotationStyle,
    AnnotationsManifest,
    SourcePtsRange,
    VisibilityPolicy,
)


class AnnotationRevisionConflict(RevisionConflict):
    code = "annotation_revision_conflict"

    def __init__(self, expected_revision: int, current_revision: int) -> None:
        RevisionConflict.__init__(
            self,
            project_id="annotation",
            expected_revision=expected_revision,
            current_revision=current_revision,
        )


@dataclass(frozen=True)
class AnnotationMutationResult:
    annotation: Annotation
    manifest_revision: int
    render_revision: int
    operation_id: str


@dataclass(frozen=True)
class AnnotationDeleteResult:
    annotation_id: str
    manifest_revision: int
    render_revision: int
    operation_id: str


def _clip_source_range(clip: ClipDefinition) -> SourcePtsRange:
    time_base = clip.analysis.get("source_time_base")
    if not isinstance(time_base, Mapping):
        raise ValueError("clip is missing an exact source time_base")
    return SourcePtsRange(
        start_pts=int(clip.analysis["source_start_pts"]),
        end_pts_exclusive=int(clip.analysis["source_end_pts_exclusive"]),
        time_base_numerator=int(time_base["numerator"]),
        time_base_denominator=int(time_base["denominator"]),
    )


def _require_clip(
    repositories: ProjectRepositories,
    project_id: str,
    clip_id: str,
    source_pts_range: SourcePtsRange,
) -> ClipDefinition:
    clips = repositories.clips.load(project_id)
    clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
    if clip is None:
        raise ValueError(f"unknown annotation clip: {clip_id}")
    clip_range = _clip_source_range(clip)
    if (
        source_pts_range.time_base_numerator != clip_range.time_base_numerator
        or source_pts_range.time_base_denominator != clip_range.time_base_denominator
    ):
        raise ValueError("annotation source time_base must match its clip")
    if (
        source_pts_range.start_pts < clip_range.start_pts
        or source_pts_range.end_pts_exclusive > clip_range.end_pts_exclusive
    ):
        raise ValueError("annotation source PTS range must stay inside its clip")
    return clip


def _stale_clip_render(
    manifest: RenderManifest,
    *,
    clip_id: str,
) -> RenderManifest:
    changed = []
    for item in manifest.clip_renders:
        if item.get("clip_id") == clip_id and item.get("status") == "success":
            changed.append(
                {
                    **item,
                    "status": "stale_input",
                    "stage": "stale_input",
                    "stale_reason": "annotation_revision_changed",
                }
            )
        else:
            changed.append(item)
    return replace(manifest, clip_renders=tuple(changed))


class AnnotationService:
    """Coordinates annotation state and render-only invalidation."""

    def __init__(
        self,
        repositories: ProjectRepositories,
        *,
        now: Callable[[], str],
        identity: Callable[[], str],
        publication_lock: RLock,
    ) -> None:
        self.repositories = repositories
        self.now = now
        self.identity = identity
        self.publication_lock = publication_lock

    def create(
        self,
        project_id: str,
        *,
        expected_revision: int,
        annotation_id: str | None,
        clip_id: str,
        anchor_type: str,
        text: str,
        content: AnnotationContent | None = None,
        panel: AnnotationPanel | None = None,
        leader: AnnotationLeader | None = None,
        anchor: Mapping[str, object],
        source_pts_range: SourcePtsRange,
        screen_offset: tuple[float, float] = (0.0, 0.0),
        style: AnnotationStyle | None = None,
        visibility_policy: VisibilityPolicy | None = None,
        user_visible: bool = True,
    ) -> AnnotationMutationResult:
        identifier = annotation_id or f"annotation-{self.identity()}"
        with self.publication_lock, self.repositories.clips.lock_for(project_id):
            _require_clip(self.repositories, project_id, clip_id, source_pts_range)
            current = self.repositories.annotations.load(project_id)
            render = self.repositories.render.load(project_id)
            if any(item.annotation_id == identifier for item in current.annotations):
                raise ValueError(f"annotation already exists: {identifier}")

            created: Annotation | None = None

            def mutate_annotations(
                manifest: AnnotationsManifest, operation_id: str
            ) -> AnnotationsManifest:
                nonlocal created
                created = Annotation.new(
                    annotation_id=identifier,
                    clip_id=clip_id,
                    anchor_type=anchor_type,
                    text=text,
                    content=content,
                    panel=panel,
                    leader=leader,
                    anchor=anchor,
                    source_pts_range=source_pts_range,
                    screen_offset=screen_offset,
                    style=style,
                    visibility_policy=visibility_policy,
                    user_visible=user_visible,
                    created_at=self.now(),
                    operation_id=operation_id,
                )
                return replace(
                    manifest,
                    annotations=(*manifest.annotations, created),
                    updated_at=self.now(),
                )

            publication = publish_manifests(
                (
                    ManifestMutation(
                        self.repositories.render,
                        project_id,
                        render.revision,
                        lambda value, _operation_id: _stale_clip_render(
                            value, clip_id=clip_id
                        ),
                    ),
                    ManifestMutation(
                        self.repositories.annotations,
                        project_id,
                        expected_revision,
                        mutate_annotations,
                    ),
                )
            )
            assert created is not None
            annotations_result = next(
                item for item in publication.manifests if item.owner == "annotations"
            )
            render_result = next(
                item for item in publication.manifests if item.owner == "render"
            )
            stored = next(
                item
                for item in getattr(annotations_result, "annotations")
                if item.annotation_id == identifier
            )
            return AnnotationMutationResult(
                annotation=stored,
                manifest_revision=annotations_result.revision,
                render_revision=render_result.revision,
                operation_id=publication.operation_id,
            )

    def update(
        self,
        project_id: str,
        annotation_id: str,
        *,
        expected_revision: int,
        expected_annotation_revision: int,
        changes: Mapping[str, object],
    ) -> AnnotationMutationResult:
        allowed = {
            "text",
            "content",
            "panel",
            "leader",
            "style",
            "source_pts_range",
            "screen_offset",
            "visibility_policy",
            "user_visible",
            "anchor",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported annotation changes: {sorted(unknown)}")
        with self.publication_lock, self.repositories.clips.lock_for(project_id):
            current = self.repositories.annotations.load(project_id)
            render = self.repositories.render.load(project_id)
            existing = next(
                (
                    item
                    for item in current.annotations
                    if item.annotation_id == annotation_id
                ),
                None,
            )
            if existing is None:
                raise FileNotFoundError(f"annotation not found: {annotation_id}")
            if existing.annotation_revision != expected_annotation_revision:
                raise AnnotationRevisionConflict(
                    expected_annotation_revision, existing.annotation_revision
                )
            if "anchor" in changes and existing.anchor_type != "cad_anchor":
                raise ValueError("video tracking anchors change only through re-anchor")

            source_range = (
                SourcePtsRange.from_dict(changes["source_pts_range"])
                if "source_pts_range" in changes
                else existing.source_pts_range
            )
            _require_clip(self.repositories, project_id, existing.clip_id, source_range)
            offset_value = changes.get("screen_offset", existing.screen_offset)
            if not isinstance(offset_value, (tuple, list)) or len(offset_value) != 2:
                raise ValueError("screen_offset must contain x and y")
            content = (
                AnnotationContent.from_dict(changes["content"])
                if "content" in changes
                else (
                    AnnotationContent(
                        title=existing.content.title,
                        body=str(changes["text"]),
                    )
                    if "text" in changes
                    else existing.content
                )
            )
            updated = replace(
                existing,
                text=content.body,
                content=content,
                panel=(
                    AnnotationPanel.from_dict(changes["panel"])
                    if "panel" in changes
                    else existing.panel
                ),
                leader=(
                    AnnotationLeader.from_dict(changes["leader"])
                    if "leader" in changes
                    else existing.leader
                ),
                style=(
                    AnnotationStyle.from_dict(changes["style"])
                    if "style" in changes
                    else existing.style
                ),
                source_pts_range=source_range,
                screen_offset=(float(offset_value[0]), float(offset_value[1])),
                visibility_policy=(
                    VisibilityPolicy.from_dict(changes["visibility_policy"])
                    if "visibility_policy" in changes
                    else existing.visibility_policy
                ),
                user_visible=(
                    bool(changes["user_visible"])
                    if "user_visible" in changes
                    else existing.user_visible
                ),
                anchor=(
                    dict(changes["anchor"]) if "anchor" in changes else existing.anchor
                ),
                annotation_revision=existing.annotation_revision + 1,
                updated_at=self.now(),
            )

            def mutate_annotations(
                manifest: AnnotationsManifest, operation_id: str
            ) -> AnnotationsManifest:
                candidate = replace(updated, updated_operation_id=operation_id)
                return replace(
                    manifest,
                    annotations=tuple(
                        candidate if item.annotation_id == annotation_id else item
                        for item in manifest.annotations
                    ),
                    updated_at=self.now(),
                )

            publication = publish_manifests(
                (
                    ManifestMutation(
                        self.repositories.render,
                        project_id,
                        render.revision,
                        lambda value, _operation_id: _stale_clip_render(
                            value, clip_id=existing.clip_id
                        ),
                    ),
                    ManifestMutation(
                        self.repositories.annotations,
                        project_id,
                        expected_revision,
                        mutate_annotations,
                    ),
                )
            )
            annotations_result = next(
                item for item in publication.manifests if item.owner == "annotations"
            )
            render_result = next(
                item for item in publication.manifests if item.owner == "render"
            )
            stored = next(
                item
                for item in getattr(annotations_result, "annotations")
                if item.annotation_id == annotation_id
            )
            return AnnotationMutationResult(
                annotation=stored,
                manifest_revision=annotations_result.revision,
                render_revision=render_result.revision,
                operation_id=publication.operation_id,
            )

    def delete(
        self,
        project_id: str,
        annotation_id: str,
        *,
        expected_revision: int,
        expected_annotation_revision: int,
    ) -> AnnotationDeleteResult:
        with self.publication_lock:
            current = self.repositories.annotations.load(project_id)
            render = self.repositories.render.load(project_id)
            existing = next(
                (
                    item
                    for item in current.annotations
                    if item.annotation_id == annotation_id
                ),
                None,
            )
            if existing is None:
                raise FileNotFoundError(f"annotation not found: {annotation_id}")
            if existing.annotation_revision != expected_annotation_revision:
                raise AnnotationRevisionConflict(
                    expected_annotation_revision, existing.annotation_revision
                )
            publication = publish_manifests(
                (
                    ManifestMutation(
                        self.repositories.render,
                        project_id,
                        render.revision,
                        lambda value, _operation_id: _stale_clip_render(
                            value, clip_id=existing.clip_id
                        ),
                    ),
                    ManifestMutation(
                        self.repositories.annotations,
                        project_id,
                        expected_revision,
                        lambda value, _operation_id: replace(
                            value,
                            annotations=tuple(
                                item
                                for item in value.annotations
                                if item.annotation_id != annotation_id
                            ),
                            updated_at=self.now(),
                        ),
                    ),
                )
            )
            annotations_result = next(
                item for item in publication.manifests if item.owner == "annotations"
            )
            render_result = next(
                item for item in publication.manifests if item.owner == "render"
            )
            return AnnotationDeleteResult(
                annotation_id=annotation_id,
                manifest_revision=annotations_result.revision,
                render_revision=render_result.revision,
                operation_id=publication.operation_id,
            )

    def activate_tracking_revision(
        self,
        project_id: str,
        annotation_id: str,
        tracking_revision: str,
        *,
        expected_revision: int,
        expected_annotation_revision: int,
    ) -> AnnotationMutationResult:
        with self.publication_lock:
            current = self.repositories.annotations.load(project_id)
            render = self.repositories.render.load(project_id)
            existing = next(
                (
                    item
                    for item in current.annotations
                    if item.annotation_id == annotation_id
                ),
                None,
            )
            if existing is None:
                raise FileNotFoundError(f"annotation not found: {annotation_id}")
            if existing.anchor_type != "video_track":
                raise ValueError("only video_track annotations own tracking revisions")
            if existing.annotation_revision != expected_annotation_revision:
                raise AnnotationRevisionConflict(
                    expected_annotation_revision, existing.annotation_revision
                )

            def mutate_annotations(
                manifest: AnnotationsManifest, operation_id: str
            ) -> AnnotationsManifest:
                updated = replace(
                    existing,
                    active_tracking_revision=tracking_revision,
                    annotation_revision=existing.annotation_revision + 1,
                    updated_at=self.now(),
                    updated_operation_id=operation_id,
                )
                return replace(
                    manifest,
                    annotations=tuple(
                        updated if item.annotation_id == annotation_id else item
                        for item in manifest.annotations
                    ),
                    updated_at=self.now(),
                )

            publication = publish_manifests(
                (
                    ManifestMutation(
                        self.repositories.render,
                        project_id,
                        render.revision,
                        lambda value, _operation_id: _stale_clip_render(
                            value, clip_id=existing.clip_id
                        ),
                    ),
                    ManifestMutation(
                        self.repositories.annotations,
                        project_id,
                        expected_revision,
                        mutate_annotations,
                    ),
                )
            )
            annotations_result = next(
                item for item in publication.manifests if item.owner == "annotations"
            )
            render_result = next(
                item for item in publication.manifests if item.owner == "render"
            )
            stored = next(
                item
                for item in getattr(annotations_result, "annotations")
                if item.annotation_id == annotation_id
            )
            return AnnotationMutationResult(
                annotation=stored,
                manifest_revision=annotations_result.revision,
                render_revision=render_result.revision,
                operation_id=publication.operation_id,
            )
