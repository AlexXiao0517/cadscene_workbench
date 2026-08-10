from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from typing import Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from .adapters import AdapterInputs, AdapterResult, WorkflowAdapterRegistry
from .analysis_adapters import (
    ADAPTER_NAME as ANALYSIS_ADAPTER_NAME,
    ADAPTER_VERSION as ANALYSIS_ADAPTER_VERSION,
    prepare_analysis_plan,
    validate_dependency_output,
    validate_result_path,
)
from .analysis_publication import AnalysisArtifactPublisher
from .executor import JobExecutionPlan
from .json_repositories import ProjectRepositories
from .models import (
    ClipDefinition,
    ClipsManifest,
    JobsManifest,
    ProjectManifest,
    RenderManifest,
    StateReference,
    activate_analysis_revision,
    register_analysis_revision,
)
from .media import (
    ProbedMedia,
    ProjectMediaSpec,
    media_compatibility,
    probe_media,
    probe_project_media_spec,
    validate_rendered_media,
)
from .identifiers import is_safe_stable_id
from .render_adapters import RenderAdapterRegistry
from .render_adapters import RenderInputs
from .source_fallback import SourceIntervalRenderAdapter, SourceIntervalRenderInputs
from .repositories import ManifestMutation, RevisionConflict, publish_manifests
from .uploads import PublishedUpload
from .queue import (
    AttemptRecord,
    LocalResourceQueue,
    PreparedSubmissionBatch,
    QueueJob,
    RestoreCleanupReservation,
)
from cadscene.video_analysis.pts import DecodedFrameTimestamp
from cadscene.workflow.job_runner import read_workflow_log_text


ANALYSIS_IDENTITY_SCHEMA = 2


def _has_exact_success_proof(job: QueueJob) -> bool:
    return (
        job.status == "success"
        and job.output_validated
        and job.validated_input_fingerprint == job.input_fingerprint
    )


@dataclass(frozen=True)
class TrajectoryPreflight:
    eligible: tuple[str, ...]
    needs_confirmation: tuple[str, ...]
    skipped: tuple[str, ...]
    reasons: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnqueueTrajectoryResult:
    enqueued_clip_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    preflight: TrajectoryPreflight


@dataclass(frozen=True)
class RenderPreflight:
    eligible: tuple[str, ...]
    confirmation_required: tuple[str, ...]
    skipped: tuple[str, ...]
    reasons: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EnqueueRenderResult:
    enqueued_clip_ids: tuple[str, ...]
    job_ids: tuple[str, ...]
    preflight: RenderPreflight


@dataclass(frozen=True)
class EnqueueAnalysisResult:
    job_ids: tuple[str, str]
    request_key: str


@dataclass(frozen=True)
class RegisterUploadResult:
    project_revision: int
    request_key: str | None
    analysis_job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActivateAnalysisResult:
    analysis_revision: str
    project_revision: int
    clips_revision: int


class _AnalysisPublicationPending(RuntimeError):
    def __init__(self, project_id: str, job_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.project_id = project_id
        self.job_id = job_id
        self.cause = cause


class _RenderPublicationPending(RuntimeError):
    def __init__(self, project_id: str, job_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.project_id = project_id
        self.job_id = job_id
        self.cause = cause


class _RenderInputStaleDuringPublication(RuntimeError):
    pass


@dataclass(frozen=True)
class _ValidatedRenderBundle:
    output_revision: str
    output_fingerprint: str
    video_path: Path
    frame_map_path: Path
    frame_map: Mapping[str, object]
    proof: Mapping[str, object]
    source_frames: tuple[DecodedFrameTimestamp, ...]
    source_time_base: Fraction
    media_spec: ProjectMediaSpec


class ProjectService:
    """The sole owner of project job-state publication."""

    def __init__(
        self,
        repositories: ProjectRepositories,
        queue: LocalResourceQueue,
        adapters: WorkflowAdapterRegistry,
        *,
        projects_root: Path,
        now: Callable[[], str],
        identity: Callable[[], str] | None = None,
        render_adapters: RenderAdapterRegistry | None = None,
        source_interval_render_adapter: SourceIntervalRenderAdapter | None = None,
        media_probe: Callable[[Path], ProbedMedia] | None = None,
        project_media_spec_probe: Callable[[Path], ProjectMediaSpec] | None = None,
    ) -> None:
        self.repositories = repositories
        self.queue = queue
        self.adapters = adapters
        self.projects_root = Path(projects_root)
        self.storage_root = self.projects_root.parent
        self.now = now
        self._identity = identity or (lambda: uuid4().hex)
        self.render_adapters = render_adapters or RenderAdapterRegistry(())
        self.source_interval_render_adapter = (
            source_interval_render_adapter or SourceIntervalRenderAdapter()
        )
        self.media_probe = media_probe or probe_media
        self.project_media_spec_probe = (
            project_media_spec_probe or probe_project_media_spec
        )
        self.analysis_publisher = AnalysisArtifactPublisher(
            storage_root=self.storage_root,
            projects_root=self.projects_root,
            identity=self._identity,
        )
        self._publication_lock = threading.RLock()
        self.queue.enable_publication_gate()

    def set_project_media_spec(
        self,
        project_id: str,
        media_spec: ProjectMediaSpec,
        *,
        media_spec_revision: str,
        expected_revision: int,
    ) -> ProjectManifest:
        if not isinstance(media_spec, ProjectMediaSpec):
            raise TypeError("media_spec must be a ProjectMediaSpec")
        if (
            not isinstance(media_spec_revision, str)
            or not media_spec_revision.strip()
            or media_spec_revision != media_spec_revision.strip()
        ):
            raise ValueError("media_spec_revision must be explicit")
        with self._state_guard(project_id):
            current = self.repositories.project.load(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            if (
                current.media_spec_revision == media_spec_revision
                and current.media_spec == media_spec.to_dict()
            ):
                return current
            return self.repositories.project.update(
                project_id,
                expected_revision=expected_revision,
                mutate=lambda value: replace(
                    value,
                    media_spec_revision=media_spec_revision,
                    media_spec=media_spec.to_dict(),
                    updated_at=self.now(),
                ),
            )

    def ensure_project_media_spec(self, project_id: str) -> ProjectManifest:
        """Create a missing project render contract from the immutable source video."""

        with self._state_guard(project_id):
            current = self.repositories.project.load(project_id)
            if _project_media_binding(current) is not None:
                return current
            source = current.source_assets.get("video")
            path_value = source.get("path") if isinstance(source, Mapping) else None
            if not isinstance(path_value, str) or not path_value:
                raise ValueError("project source video is unavailable")
            spec = self.project_media_spec_probe(Path(path_value))
            return self.repositories.project.update(
                project_id,
                expected_revision=current.revision,
                mutate=lambda value: replace(
                    value,
                    media_spec_revision=_media_spec_revision(spec),
                    media_spec=spec.to_dict(),
                    updated_at=self.now(),
                ),
            )

    def register_uploaded_asset(
        self,
        project_id: str,
        upload: PublishedUpload,
        *,
        expected_revision: int | None,
    ) -> RegisterUploadResult:
        """Atomically register immutable upload state without starting analysis."""

        if upload.project_id != project_id:
            raise ValueError("published upload belongs to another project")
        if not upload.path.is_file() or not upload.validation_report_path.is_file():
            raise FileNotFoundError("immutable upload media/report is unavailable")
        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            if expected_revision is not None and project.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=project.revision,
                )
            assets = dict(project.source_assets)
            assets[upload.asset_type] = {
                "path": str(upload.path),
                "original_filename": upload.original_filename,
                "size_bytes": upload.size_bytes,
                "sha256": upload.sha256,
                "validation": dict(upload.validation),
                "validation_report": str(upload.validation_report_path),
            }
            clears_media_spec = upload.asset_type == "video"
            request_key = _analysis_request_key_from_assets(assets)
            try:
                publication = publish_manifests(
                    [
                        ManifestMutation(
                            repository=self.repositories.project,
                            project_id=project_id,
                            expected_revision=project.revision,
                            mutate=lambda value, _operation_id: replace(
                                value,
                                updated_at=self.now(),
                                source_assets=assets,
                                media_spec_revision=(
                                    None
                                    if clears_media_spec
                                    else value.media_spec_revision
                                ),
                                media_spec=(
                                    None if clears_media_spec else value.media_spec
                                ),
                            ),
                        )
                    ]
                )
            except Exception as publication_error:
                from .recovery import reconcile_project

                reconcile_project(project_id, repositories=self.repositories)
                recovered_project = self.repositories.project.load(project_id)
                recovered_asset = recovered_project.source_assets.get(
                    upload.asset_type
                )
                asset_recovered = (
                    isinstance(recovered_asset, Mapping)
                    and recovered_asset.get("path") == str(upload.path)
                    and recovered_asset.get("sha256") == upload.sha256
                )
                if not asset_recovered:
                    raise publication_error
                return RegisterUploadResult(
                    project_revision=recovered_project.revision,
                    request_key=request_key,
                    analysis_job_ids=(),
                )
            updated_project = next(
                item
                for item in publication.manifests
                if isinstance(item, ProjectManifest)
            )
            return RegisterUploadResult(
                project_revision=updated_project.revision,
                request_key=request_key,
                analysis_job_ids=(),
            )

    def request_reanalysis(
        self, project_id: str, *, expected_revision: int
    ) -> RegisterUploadResult:
        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            if project.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=project.revision,
                )
            base_key = _analysis_request_key_from_assets(project.source_assets)
            if base_key is None:
                raise ValueError("validated video and CAD assets are required")
            for required in ("video", "cad"):
                descriptor = project.source_assets.get(required)
                if not isinstance(descriptor, Mapping):
                    raise ValueError(f"validated {required} asset is required")
                media = Path(str(descriptor.get("path") or ""))
                report = Path(str(descriptor.get("validation_report") or ""))
                if not media.is_file() or not report.is_file():
                    raise ValueError(
                        f"immutable {required} media/report is unavailable"
                    )
            request_key = f"{base_key}:manual:{self._identity()}"
            batch = self._prepare_analysis_submission(
                project_id,
                request_key=request_key,
                project_assets=project.source_assets,
            )
            prepared = batch.jobs
            job_ids = batch.job_ids
            current_jobs = self.repositories.jobs.load(project_id)
            order = tuple(
                dict.fromkeys(
                    (
                        *(
                            job_id
                            for job_id in self.queue.queue_order()
                            if self.queue.get(job_id).project_id == project_id
                        ),
                        *job_ids,
                    )
                )
            )
            by_id = {
                item.job_id: item
                for item in self.queue.jobs()
                if item.project_id == project_id
            }
            by_id.update({item.job_id: item for item in prepared})

            def mutate_project(
                value: ProjectManifest, operation_id: str
            ) -> ProjectManifest:
                assets = dict(value.source_assets)
                base_state = {
                    "requested_at": self.now(),
                    "request_kind": "manual",
                }
                state, project_state = self._analysis_state_for_submission(
                    batch=batch,
                    project=value,
                    project_assets=value.source_assets,
                    request_key=request_key,
                    base_state=base_state,
                    operation_id=operation_id,
                )
                assets["_analysis"] = state
                return replace(
                    value,
                    updated_at=self.now(),
                    source_assets=assets,
                    project_state=project_state,
                )

            def mutate_jobs(
                value: JobsManifest, operation_id: str
            ) -> JobsManifest:
                return replace(
                    value,
                    updated_at=self.now(),
                    queue_order=order,
                    jobs=self._analysis_submission_payload(
                        by_id=by_id,
                        order=order,
                        batch=batch,
                        operation_id=operation_id,
                    ),
                )

            try:
                publication = publish_manifests(
                    (
                        ManifestMutation(
                            repository=self.repositories.project,
                            project_id=project_id,
                            expected_revision=project.revision,
                            mutate=mutate_project,
                        ),
                        ManifestMutation(
                            repository=self.repositories.jobs,
                            project_id=project_id,
                            expected_revision=current_jobs.revision,
                            mutate=mutate_jobs,
                        ),
                    )
                )
            except Exception as publication_error:
                from .recovery import reconcile_project

                reconcile_project(project_id, repositories=self.repositories)
                recovered_project = self.repositories.project.load(project_id)
                recovered_state = recovered_project.source_assets.get("_analysis")
                recovered_jobs = self.repositories.jobs.load(project_id)
                recovered_by_id = {
                    str(item["job_id"]): QueueJob.from_dict(item)
                    for item in recovered_jobs.jobs
                }
                request_recovered = (
                    isinstance(recovered_state, Mapping)
                    and recovered_state.get("request_key") == request_key
                    and tuple(recovered_state.get("job_ids", ())) == job_ids
                )
                if not request_recovered or not all(
                    job_id in recovered_by_id for job_id in job_ids
                ):
                    raise publication_error
                self._commit_analysis_submission(batch, recovered_by_id)
                self.queue.acknowledge_publication(project_id)
                return RegisterUploadResult(
                    project_revision=recovered_project.revision,
                    request_key=request_key,
                    analysis_job_ids=job_ids,
                )
            persisted_jobs = next(
                item
                for item in publication.manifests
                if isinstance(item, JobsManifest)
            )
            persisted = {
                str(item["job_id"]): QueueJob.from_dict(item)
                for item in persisted_jobs.jobs
            }
            self._commit_analysis_submission(batch, persisted)
            self.queue.acknowledge_publication(project_id)
            updated_project = next(
                item
                for item in publication.manifests
                if isinstance(item, ProjectManifest)
            )
            return RegisterUploadResult(
                project_revision=updated_project.revision,
                request_key=request_key,
                analysis_job_ids=job_ids,
            )

    def activate_candidate_analysis(
        self,
        project_id: str,
        *,
        candidate_analysis_revision: str,
        expected_project_revision: int,
        expected_clips_revision: int,
    ) -> ActivateAnalysisResult:
        """Explicitly activate one immutable candidate analysis revision."""

        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            existing_clips = self.repositories.clips.load(project_id)
            if project.revision != expected_project_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_project_revision,
                    current_revision=project.revision,
                )
            if existing_clips.revision != expected_clips_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_clips_revision,
                    current_revision=existing_clips.revision,
                )
            if project.candidate_analysis_revision != candidate_analysis_revision:
                raise ValueError("candidate analysis revision is no longer current")

            revision_descriptors = project.source_assets.get("_analysis_revisions")
            if not isinstance(revision_descriptors, Mapping):
                raise ValueError("analysis revision descriptors are unavailable")
            descriptor = revision_descriptors.get(candidate_analysis_revision)
            if not isinstance(descriptor, Mapping):
                raise ValueError("candidate analysis descriptor is unavailable")
            input_snapshot = descriptor.get("input_snapshot")
            if not isinstance(input_snapshot, Mapping):
                raise ValueError("candidate analysis input snapshot is unavailable")
            artifact_root = Path(str(descriptor.get("analysis_artifact_path") or ""))
            clip_manifest_path = (
                artifact_root / "02_video_analysis" / "clip_manifest.json"
            )
            if not clip_manifest_path.is_file():
                raise FileNotFoundError("candidate clip manifest is unavailable")
            payload = json.loads(clip_manifest_path.read_text(encoding="utf-8"))
            if payload.get("analysis_revision") != candidate_analysis_revision:
                raise ValueError("candidate clip manifest revision does not match")
            candidate_clips = tuple(
                ClipDefinition.from_analysis(
                    {**item, "input_snapshot": dict(input_snapshot)},
                    generated_display_name=(
                        f"场景 {int(item.get('scene_index', 1)):02d} · "
                        f"第 {int(item.get('segment_index', 1))} 段"
                    ),
                )
                for item in payload.get("clips", ())
            )
            candidate_manifest = ClipsManifest.new(
                project_id,
                analysis_revision=candidate_analysis_revision,
                clips=candidate_clips,
                updated_at=self.now(),
            )

            def mutate_project(
                value: ProjectManifest, operation_id: str
            ) -> ProjectManifest:
                activated, _ = activate_analysis_revision(
                    value,
                    existing_clips,
                    candidate_manifest,
                    operation_id=operation_id,
                )
                return replace(
                    activated,
                    updated_at=self.now(),
                    project_state="ready",
                )

            def mutate_clips(
                value: ClipsManifest, operation_id: str
            ) -> ClipsManifest:
                _, activated = activate_analysis_revision(
                    project,
                    value,
                    candidate_manifest,
                    operation_id=operation_id,
                )
                return replace(
                    activated,
                    revision=value.revision,
                    updated_at=self.now(),
                )

            publication = publish_manifests(
                (
                    ManifestMutation(
                        repository=self.repositories.project,
                        project_id=project_id,
                        expected_revision=expected_project_revision,
                        mutate=mutate_project,
                    ),
                    ManifestMutation(
                        repository=self.repositories.clips,
                        project_id=project_id,
                        expected_revision=expected_clips_revision,
                        mutate=mutate_clips,
                    ),
                )
            )
            published_project = next(
                item
                for item in publication.manifests
                if isinstance(item, ProjectManifest)
            )
            published_clips = next(
                item
                for item in publication.manifests
                if isinstance(item, ClipsManifest)
            )
            return ActivateAnalysisResult(
                analysis_revision=candidate_analysis_revision,
                project_revision=published_project.revision,
                clips_revision=published_clips.revision,
            )

    def enqueue_analysis_jobs(self, project_id: str) -> EnqueueAnalysisResult:
        """Persist the CAD -> video lightweight analysis DAG.

        The queue owns execution state; the project manifest only records the
        current request and references to the durable jobs.
        """

        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            analysis = project.source_assets.get("_analysis")
            if not isinstance(analysis, Mapping):
                raise ValueError("project has no analysis request")
            request_key = str(analysis.get("request_key") or "")
            if not request_key:
                raise ValueError("analysis request_key is missing")
            for required in ("video", "cad"):
                asset = project.source_assets.get(required)
                if not isinstance(asset, Mapping) or not asset.get("path") or not asset.get("sha256"):
                    raise ValueError(f"published {required} asset is missing")

            recorded_ids = tuple(str(item) for item in analysis.get("job_ids", ()))
            batch: PreparedSubmissionBatch | None = None
            if len(recorded_ids) == 2:
                queue_jobs = {item.job_id: item for item in self.queue.jobs()}
                recorded = tuple(queue_jobs.get(job_id) for job_id in recorded_ids)
                reused = self._validated_analysis_dag(
                    tuple(item for item in recorded if item is not None),
                    project_id=project_id,
                    request_key=request_key,
                    project_assets=project.source_assets,
                )
                if reused is not None and len(recorded) == len(reused):
                    batch = PreparedSubmissionBatch(
                        jobs=reused,
                        new_candidates=(),
                        reused_jobs=reused,
                    )

            if batch is None:
                batch = self._prepare_analysis_submission(
                    project_id,
                    request_key=request_key,
                    project_assets=project.source_assets,
                )
            prepared = batch.jobs
            job_ids = batch.job_ids
            current_jobs = self.repositories.jobs.load(project_id)
            existing_jobs = tuple(
                item for item in self.queue.jobs() if item.project_id == project_id
            )
            by_id = {item.job_id: item for item in existing_jobs}
            for candidate in prepared:
                by_id[candidate.job_id] = candidate
            queue_order = tuple(dict.fromkeys((
                *(
                job_id
                for job_id in self.queue.queue_order()
                if self.queue.get(job_id).project_id == project_id
                ),
                *(item.job_id for item in prepared),
            )))
            def mutate_project(
                value: ProjectManifest, publication_operation_id: str
            ) -> ProjectManifest:
                assets = dict(value.source_assets)
                state, project_state = self._analysis_state_for_submission(
                    batch=batch,
                    project=value,
                    project_assets=assets,
                    request_key=request_key,
                    base_state=(
                        assets.get("_analysis")
                        if isinstance(assets.get("_analysis"), Mapping)
                        else None
                    ),
                    operation_id=publication_operation_id,
                )
                assets["_analysis"] = state
                return replace(
                    value,
                    updated_at=self.now(),
                    source_assets=assets,
                    project_state=project_state,
                )

            def mutate_jobs(
                value: JobsManifest, publication_operation_id: str
            ) -> JobsManifest:
                stamped_jobs = self._analysis_submission_payload(
                    by_id=by_id,
                    order=queue_order,
                    batch=batch,
                    operation_id=publication_operation_id,
                )
                return replace(
                    value,
                    jobs=stamped_jobs,
                    queue_order=queue_order,
                    updated_at=self.now(),
                )

            try:
                publication = publish_manifests(
                    (
                        ManifestMutation(
                            repository=self.repositories.project,
                            project_id=project_id,
                            expected_revision=project.revision,
                            mutate=mutate_project,
                        ),
                        ManifestMutation(
                            repository=self.repositories.jobs,
                            project_id=project_id,
                            expected_revision=current_jobs.revision,
                            mutate=mutate_jobs,
                        ),
                    )
                )
            except Exception as publication_error:
                from .recovery import reconcile_project

                reconcile_project(project_id, repositories=self.repositories)
                recovered_project = self.repositories.project.load(project_id)
                recovered_jobs = self.repositories.jobs.load(project_id)
                recovered_by_id = {
                    str(item["job_id"]): QueueJob.from_dict(item)
                    for item in recovered_jobs.jobs
                }
                recovered_state = recovered_project.source_assets.get("_analysis")
                request_recovered = (
                    isinstance(recovered_state, Mapping)
                    and recovered_state.get("request_key") == request_key
                    and tuple(recovered_state.get("job_ids", ())) == job_ids
                    and all(job_id in recovered_by_id for job_id in job_ids)
                )
                if request_recovered:
                    committed = self._commit_analysis_submission(
                        batch, recovered_by_id
                    )
                    self.queue.acknowledge_publication(project_id)
                    return EnqueueAnalysisResult(
                        job_ids=(committed[0].job_id, committed[1].job_id),
                        request_key=request_key,
                    )
                if (
                    isinstance(recovered_state, Mapping)
                    and recovered_state.get("request_key") == request_key
                    and recovered_state.get("status") == "queued"
                ):
                    assets = dict(recovered_project.source_assets)
                    failed_state = dict(recovered_state)
                    failed_state.update(
                        {
                            "status": "failed",
                            "job_ids": [],
                            "error": f"analysis enqueue publication failed: {publication_error}",
                        }
                    )
                    assets["_analysis"] = failed_state
                    self.repositories.project.update(
                        project_id,
                        expected_revision=recovered_project.revision,
                        mutate=lambda value: replace(
                            value,
                            updated_at=self.now(),
                            source_assets=assets,
                            project_state="analysis_failed",
                        ),
                    )
                raise publication_error
            persisted_jobs = next(
                item
                for item in publication.manifests
                if isinstance(item, JobsManifest)
            )
            persisted_by_id = {
                str(item["job_id"]): QueueJob.from_dict(item)
                for item in persisted_jobs.jobs
            }
            committed = self._commit_analysis_submission(
                batch, persisted_by_id
            )
            self.queue.acknowledge_publication(project_id)
            return EnqueueAnalysisResult(
                job_ids=(committed[0].job_id, committed[1].job_id),
                request_key=request_key,
            )

    def preflight_trajectory_jobs(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None = None,
    ) -> TrajectoryPreflight:
        project = self.repositories.project.load(project_id)
        clips_manifest = self.repositories.clips.load(project_id)
        selected = _select_clips(clips_manifest.clips, clip_ids)
        analysis = project.source_assets.get("_analysis")
        if (
            isinstance(analysis, Mapping)
            and analysis.get("status")
            in {"queued", "preparing", "running", "validating"}
        ):
            skipped = tuple(clip.clip_id for clip in selected)
            return TrajectoryPreflight(
                eligible=(),
                needs_confirmation=(),
                skipped=skipped,
                reasons={
                    clip_id: "project analysis is still running"
                    for clip_id in skipped
                },
            )
        eligible: list[str] = []
        confirmation: list[str] = []
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        for clip in selected:
            video_path = _clip_asset_path(clip, project.source_assets, "video")
            srt_path = _clip_asset_path(clip, project.source_assets, "srt")
            workflow = clip.resolved_workflow
            if workflow is None:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = "clip has no resolved workflow"
                continue
            try:
                adapter = self.adapters.for_workflow(workflow)
            except KeyError:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = f"unsupported workflow: {workflow}"
                continue
            if not adapter.available:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = (
                    adapter.unavailable_reason or "adapter unavailable"
                )
                continue
            if adapter.requires_physical_mp4 and (
                video_path is None or not video_path.is_file()
            ):
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = "physical MP4 source is missing"
                continue
            if adapter.srt_requirement != "none" and (
                srt_path is None or not srt_path.is_file()
            ):
                skipped.append(clip.clip_id)
                reasons[
                    clip.clip_id
                ] = f"physical SRT with {adapter.srt_requirement} coverage is missing"
                continue
            if bool(clip.analysis.get("needs_review", False)):
                confirmation.append(clip.clip_id)
                reasons[clip.clip_id] = "clip analysis requires confirmation"
            else:
                eligible.append(clip.clip_id)
        return TrajectoryPreflight(
            eligible=tuple(eligible),
            needs_confirmation=tuple(confirmation),
            skipped=tuple(skipped),
            reasons=reasons,
        )

    def preflight_render_jobs(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None = None,
    ) -> RenderPreflight:
        project = self.repositories.project.load(project_id)
        clips_manifest = self.repositories.clips.load(project_id)
        jobs_manifest = self.repositories.jobs.load(project_id)
        selected = _select_clips(clips_manifest.clips, clip_ids)
        stored_jobs = tuple(QueueJob.from_dict(item) for item in jobs_manifest.jobs)
        eligible: list[str] = []
        confirmation: list[str] = []
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        media_binding = _project_media_binding(project)
        if media_binding is None:
            clip_ids_without_spec = tuple(clip.clip_id for clip in selected)
            return RenderPreflight(
                eligible=(),
                confirmation_required=(),
                skipped=clip_ids_without_spec,
                reasons={
                    clip_id: "project media specification is unavailable"
                    for clip_id in clip_ids_without_spec
                },
            )
        for clip in selected:
            workflow = clip.resolved_workflow
            if workflow is None:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = "clip has no resolved workflow"
                continue
            try:
                self.render_adapters.for_workflow(workflow)
            except KeyError:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = (
                    f"render adapter is unavailable for workflow: {workflow}"
                )
                continue
            try:
                physical_video, physical_map = _render_physical_inputs(
                    clip, stored_jobs
                )
                _render_input_asset_identity(
                    clip, video_path=physical_video, frame_map_path=physical_map
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = (
                    f"physical render input validation failed: {exc}"
                )
                continue
            trajectory = self._current_trajectory_for_render(clip, stored_jobs)
            if trajectory is None:
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = (
                    "current trajectory has no exact validated success proof"
                )
                continue
            workbench = _saved_workbench_reference(clip)
            if workbench is None or not _workbench_binds_trajectory(
                clip, trajectory, workbench.value
            ):
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = (
                    "clip has no saved workbench output bound to current trajectory"
                )
                continue
            if not _validate_workbench_immutable_output(
                self.projects_root, project_id, clip, workbench
            ):
                skipped.append(clip.clip_id)
                reasons[clip.clip_id] = "immutable workbench output validation failed"
                continue
            if bool(clip.analysis.get("needs_review", False)):
                confirmation.append(clip.clip_id)
                reasons[clip.clip_id] = "clip analysis requires confirmation"
            else:
                eligible.append(clip.clip_id)
        return RenderPreflight(
            eligible=tuple(eligible),
            confirmation_required=tuple(confirmation),
            skipped=tuple(skipped),
            reasons=reasons,
        )

    def enqueue_render_jobs(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None = None,
        confirmed_clip_ids: Sequence[str] = (),
        expected_jobs_revision: int | None = None,
    ) -> EnqueueRenderResult:
        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            preflight = self.preflight_render_jobs(project_id, clip_ids=clip_ids)
            confirmed = set(confirmed_clip_ids)
            invalid_confirmations = confirmed - set(preflight.confirmation_required)
            if invalid_confirmations:
                raise ValueError(
                    f"clips do not require confirmation: {sorted(invalid_confirmations)}"
                )
            accepted_ids = (
                *preflight.eligible,
                *(
                    clip_id
                    for clip_id in preflight.confirmation_required
                    if clip_id in confirmed
                ),
            )
            clips_manifest = self.repositories.clips.load(project_id)
            jobs_manifest = self.repositories.jobs.load(project_id)
            project = self.repositories.project.load(project_id)
            media_binding = _project_media_binding(project)
            if media_binding is None:
                raise RuntimeError("project media specification changed after preflight")
            media_spec_revision, media_spec = media_binding
            by_id = {clip.clip_id: clip for clip in clips_manifest.clips}
            stored_jobs = tuple(
                QueueJob.from_dict(item) for item in jobs_manifest.jobs
            )
            job_ids: list[str] = []
            for clip_id in accepted_ids:
                clip = by_id[clip_id]
                trajectory = self._current_trajectory_for_render(clip, stored_jobs)
                workbench = _saved_workbench_reference(clip)
                if (
                    trajectory is None
                    or workbench is None
                    or not _workbench_binds_trajectory(
                        clip, trajectory, workbench.value
                    )
                    or not _validate_workbench_immutable_output(
                        self.projects_root, project_id, clip, workbench
                    )
                ):
                    raise RuntimeError("render prerequisites changed after preflight")
                adapter = self.render_adapters.for_workflow(
                    str(clip.resolved_workflow)
                )
                physical_video, physical_map = _render_physical_inputs(
                    clip, stored_jobs
                )
                render = self._new_render_job(
                    project_id,
                    clip,
                    trajectory=trajectory,
                    workbench=workbench,
                    adapter_name=adapter.name,
                    adapter_version=adapter.version,
                    project_revision=project.revision,
                    clips_revision=clips_manifest.revision,
                    media_spec=media_spec,
                    media_spec_revision=media_spec_revision,
                    physical_video_path=physical_video,
                    physical_frame_map_path=physical_map,
                )
                submitted = self.queue.submit(render)
                if submitted.job_id == render.job_id:
                    Path(submitted.attempts[-1].directory).mkdir(
                        parents=True, exist_ok=False
                    )
                job_ids.append(submitted.job_id)
            if job_ids:
                self._publish_queue_locked(project_id)
            return EnqueueRenderResult(
                enqueued_clip_ids=tuple(accepted_ids),
                job_ids=tuple(job_ids),
                preflight=preflight,
            )

    def _current_trajectory_for_render(
        self,
        clip: ClipDefinition,
        jobs: Sequence[QueueJob],
    ) -> QueueJob | None:
        for job in reversed(jobs):
            if (
                job.job_type != "trajectory"
                or job.clip_id != clip.clip_id
                or job.adapter_name != clip.resolved_workflow
                or job.input_revision != clip.analysis_revision
                or not _has_exact_success_proof(job)
                or not job.output_revision
                or not job.output_fingerprint
                or not _trajectory_artifact_matches_proof(job)
            ):
                continue
            if self._current_input_fingerprint(job) == job.input_fingerprint:
                return job
        return None

    def enqueue_trajectory_jobs(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None = None,
        confirmed_clip_ids: Sequence[str] = (),
        expected_jobs_revision: int | None = None,
    ) -> EnqueueTrajectoryResult:
        with self._state_guard(project_id):
            if expected_jobs_revision is not None:
                current_jobs = self.repositories.jobs.load(project_id)
                if current_jobs.revision != expected_jobs_revision:
                    raise RevisionConflict(
                        project_id=project_id,
                        expected_revision=expected_jobs_revision,
                        current_revision=current_jobs.revision,
                    )
            return self._enqueue_trajectory_jobs_locked(
                project_id,
                clip_ids=clip_ids,
                confirmed_clip_ids=confirmed_clip_ids,
            )

    def enqueue_workbench_clip_export(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_jobs_revision: int,
    ) -> QueueJob:
        """Ensure one on-demand physical clip export is queued for workbench entry."""

        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            project = self.repositories.project.load(project_id)
            clips = self.repositories.clips.load(project_id)
            clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
            if clip is None:
                raise KeyError(f"unknown clip ID: {clip_id}")
            for existing in reversed(self.queue.jobs()):
                if (
                    existing.project_id == project_id
                    and existing.clip_id == clip_id
                    and existing.job_type == "clip_export"
                    and existing.status
                    in {"queued", "preparing", "running", "validating", "success"}
                    and self._current_input_fingerprint(existing)
                    == existing.input_fingerprint
                ):
                    return existing
            export = self._new_export_job(
                project_id,
                clip,
                project_assets=project.source_assets,
                project_revision=project.revision,
                clips_revision=clips.revision,
            )
            submitted = self.queue.submit(export)
            if submitted.job_id == export.job_id:
                Path(submitted.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            self._publish_queue_locked(project_id)
            return submitted

    def update_clip_workflow(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int,
        workflow_override: str | None,
    ) -> ClipsManifest:
        """Change the user layer and stale old derived references without deletion."""

        allowed = {"sfm_only", "srt_sfm_fused", "srt_full_pose", "pure_rotation"}
        if workflow_override is not None and workflow_override not in allowed:
            raise ValueError(f"unsupported workflow override: {workflow_override}")
        with self._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            by_id = {clip.clip_id: clip for clip in current.clips}
            if clip_id not in by_id:
                raise KeyError(f"unknown clip ID: {clip_id}")
            selected = by_id[clip_id]
            if selected.workflow_override == workflow_override:
                return current
            render = self.repositories.render.load(project_id)

            def mutate_clips(value: ClipsManifest, _operation_id: str) -> ClipsManifest:
                changed: list[ClipDefinition] = []
                for clip in value.clips:
                    if clip.clip_id != clip_id:
                        changed.append(clip)
                        continue
                    references = tuple(
                        replace(
                            reference,
                            value={
                                **reference.value,
                                "previous_status": reference.value.get("status"),
                                "status": "stale",
                                "stale_reason": "workflow_changed",
                            },
                        )
                        for reference in clip.references
                    )
                    changed.append(
                        replace(
                            clip.with_workflow_override(workflow_override),
                            references=references,
                        )
                    )
                return replace(value, clips=tuple(changed), updated_at=self.now())

            def mutate_render(
                value: RenderManifest, _operation_id: str
            ) -> RenderManifest:
                changed = tuple(
                    {
                        **item,
                        "previous_status": item.get("status"),
                        "status": "stale",
                        "stale_reason": "workflow_changed",
                    }
                    if item.get("clip_id") == clip_id
                    else item
                    for item in value.clip_renders
                )
                return replace(value, clip_renders=changed, updated_at=self.now())

            mutations = [
                ManifestMutation(
                    repository=self.repositories.clips,
                    project_id=project_id,
                    expected_revision=current.revision,
                    mutate=mutate_clips,
                )
            ]
            if any(item.get("clip_id") == clip_id for item in render.clip_renders):
                mutations.append(
                    ManifestMutation(
                        repository=self.repositories.render,
                        project_id=project_id,
                        expected_revision=render.revision,
                        mutate=mutate_render,
                    )
                )
            publish_manifests(mutations)
            return self.repositories.clips.load(project_id)

    def update_clip_display_name(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int,
        custom_display_name: str | None,
    ) -> ClipsManifest:
        if custom_display_name is not None:
            custom_display_name = custom_display_name.strip()
            if not custom_display_name or len(custom_display_name) > 120:
                raise ValueError("custom_display_name must contain 1 to 120 characters")
        with self._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            if not any(clip.clip_id == clip_id for clip in current.clips):
                raise KeyError(f"unknown clip ID: {clip_id}")
            if next(
                clip for clip in current.clips if clip.clip_id == clip_id
            ).custom_display_name == custom_display_name:
                return current
            return self.repositories.clips.update(
                project_id,
                expected_revision=expected_revision,
                mutate=lambda value: replace(
                    value,
                    updated_at=self.now(),
                    clips=tuple(
                        clip.with_custom_display_name(custom_display_name)
                        if clip.clip_id == clip_id
                        else clip
                        for clip in value.clips
                    ),
                ),
            )

    def update_project_display_name(
        self,
        project_id: str,
        *,
        expected_revision: int,
        display_name: str,
    ) -> ProjectManifest:
        normalized = display_name.strip()
        if not normalized or len(normalized) > 120:
            raise ValueError("display_name must contain 1 to 120 characters")
        current = self.repositories.project.load(project_id)
        if current.revision != expected_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected_revision,
                current_revision=current.revision,
            )
        if current.source_assets.get("display_name") == normalized:
            return current
        return self.repositories.project.update(
            project_id,
            expected_revision=expected_revision,
            mutate=lambda value: replace(
                value,
                updated_at=self.now(),
                source_assets={**value.source_assets, "display_name": normalized},
            ),
        )

    def _enqueue_trajectory_jobs_locked(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None,
        confirmed_clip_ids: Sequence[str],
    ) -> EnqueueTrajectoryResult:
        preflight = self.preflight_trajectory_jobs(project_id, clip_ids=clip_ids)
        confirmed = set(confirmed_clip_ids)
        invalid_confirmations = confirmed - set(preflight.needs_confirmation)
        if invalid_confirmations:
            raise ValueError(
                f"clips do not require confirmation: {sorted(invalid_confirmations)}"
            )
        accepted_ids = (
            *preflight.eligible,
            *(item for item in preflight.needs_confirmation if item in confirmed),
        )
        clips_manifest = self.repositories.clips.load(project_id)
        project = self.repositories.project.load(project_id)
        by_id = {clip.clip_id: clip for clip in clips_manifest.clips}
        trajectory_ids: list[str] = []
        needs_export = [
            by_id[clip_id]
            for clip_id in accepted_ids
            if (
                _clip_output_path(by_id[clip_id]) is None
                or not _clip_output_path(by_id[clip_id]).is_file()
            )
        ]
        export_dependencies: dict[str, tuple[str, ...]] = {}
        for export_clip in needs_export:
            export = self._new_export_job(
                project_id,
                export_clip,
                project_assets=project.source_assets,
                project_revision=project.revision,
                clips_revision=clips_manifest.revision,
            )
            submitted_export = self.queue.submit(export)
            if submitted_export.job_id == export.job_id:
                Path(submitted_export.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            export_dependencies[export_clip.clip_id] = (submitted_export.job_id,)
        for clip_id in accepted_ids:
            clip = by_id[clip_id]
            adapter = self.adapters.for_workflow(str(clip.resolved_workflow))
            physical_clip = _clip_output_path(clip)
            dependency_ids = (
                export_dependencies[clip.clip_id]
                if physical_clip is None or not physical_clip.is_file()
                else ()
            )
            solve = self._new_job(
                project_id,
                clip,
                job_type="trajectory",
                resource_class="heavy_compute",
                adapter_name=adapter.name,
                adapter_version=adapter.version,
                exclusive_key=f"trajectory:{project_id}:{clip.clip_id}",
                dependency_ids=dependency_ids,
                project_assets=project.source_assets,
                project_revision=project.revision,
                clips_revision=clips_manifest.revision,
            )
            submitted_solve = self.queue.submit(solve)
            if submitted_solve.job_id == solve.job_id:
                Path(submitted_solve.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            trajectory_ids.append(submitted_solve.job_id)
        self._publish_queue_locked(project_id)
        return EnqueueTrajectoryResult(
            enqueued_clip_ids=tuple(accepted_ids),
            job_ids=tuple(trajectory_ids),
            preflight=preflight,
        )

    def finish_job(
        self,
        project_id: str,
        job_id: str,
        result: AdapterResult,
        *,
        current_fingerprint: str | None = None,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        with self._publication_lock:
            try:
                return self._finish_job_once(
                    project_id,
                    job_id,
                    result,
                    current_fingerprint=current_fingerprint,
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
            except _AnalysisPublicationPending as pending:
                return self._recover_analysis_publication(pending)
            except _RenderPublicationPending as pending:
                return self._recover_render_publication(pending)

    def _finish_job_once(
        self,
        project_id: str,
        job_id: str,
        result: AdapterResult,
        *,
        current_fingerprint: str | None,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            authoritative_fingerprint = self._current_input_fingerprint(current)
            lease = {
                "attempt_number": attempt_number,
                "claim_token": claim_token,
            }
            if current.job_type in {"cad_analysis", "video_analysis"}:
                return self._finish_analysis_job_locked(
                    current, result, lease=lease
                )
            if current.job_type == "clip_render":
                return self._finish_render_job_locked(
                    current,
                    result,
                    authoritative_fingerprint=authoritative_fingerprint,
                    lease=lease,
                )
            if authoritative_fingerprint != current.input_fingerprint:
                finished = self.queue.mark_stale_input(job_id, **lease)
            elif result.status != "success":
                finished = self.queue.mark_failed(
                    job_id, result.error or "adapter execution failed", **lease
                )
            else:
                if result.output_revision is None or result.output_fingerprint is None:
                    finished = self.queue.mark_failed(
                        job_id,
                        "adapter returned an unvalidated output identity",
                        **lease,
                    )
                else:
                    if current.status == "running":
                        self.queue.mark_validating(job_id, **lease)
                    finished = self.queue.mark_success(
                        job_id,
                        output_revision=result.output_revision,
                        output_fingerprint=result.output_fingerprint,
                        output_validated=True,
                        published_outputs=result.outputs,
                        **lease,
                    )
            self._publish_queue_locked(project_id)
            return finished

    def _finish_render_job_locked(
        self,
        current: QueueJob,
        result: AdapterResult,
        *,
        authoritative_fingerprint: str | None,
        lease: Mapping[str, object],
    ) -> QueueJob:
        kwargs = {
            "attempt_number": int(lease["attempt_number"]),
            "claim_token": str(lease["claim_token"]),
        }
        if authoritative_fingerprint != current.input_fingerprint:
            finished = self.queue.mark_stale_input(current.job_id, **kwargs)
            self._publish_queue_locked(current.project_id)
            return finished
        if result.status != "success":
            finished = self.queue.mark_failed(
                current.job_id,
                result.error or "render adapter execution failed",
                **kwargs,
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if current.status == "running":
            self.queue.mark_validating(current.job_id, **kwargs)
        try:
            validated = self._validate_render_result(current, result)
            if self._current_input_fingerprint(current) != current.input_fingerprint:
                finished = self.queue.mark_stale_input(current.job_id, **kwargs)
                self._publish_queue_locked(current.project_id)
                return finished
            published_outputs = self._publish_render_artifacts(current, validated)
            if self._current_input_fingerprint(current) != current.input_fingerprint:
                finished = self.queue.mark_stale_input(current.job_id, **kwargs)
                self._publish_queue_locked(current.project_id)
                return finished
            candidate = self.queue.prepare_success_candidate(
                current.job_id,
                output_revision=validated.output_revision,
                output_fingerprint=validated.output_fingerprint,
                output_validated=True,
                published_outputs=published_outputs,
                validation_proof=validated.proof,
                **kwargs,
            )
            jobs_manifest = self.repositories.jobs.load(current.project_id)
            render_manifest = self.repositories.render.load(current.project_id)
            clips_manifest = self.repositories.clips.load(current.project_id)
            clip = next(
                item
                for item in clips_manifest.clips
                if item.clip_id == current.clip_id
            )

            def mutate_jobs(value: JobsManifest, operation_id: str) -> JobsManifest:
                if self._current_input_fingerprint(current) != current.input_fingerprint:
                    raise _RenderInputStaleDuringPublication
                return replace(
                    value,
                    jobs=tuple(
                        replace(
                            candidate,
                            operation_id=operation_id,
                            publication_operation_id=operation_id,
                        ).to_dict()
                        if item.get("job_id") == current.job_id
                        else dict(item)
                        for item in value.jobs
                    ),
                    updated_at=self.now(),
                )

            render_state = {
                "render_id": f"{current.clip_id}:{validated.output_revision}",
                "project_id": current.project_id,
                "clip_id": current.clip_id,
                "job_id": current.job_id,
                "status": "success",
                "workflow": clip.resolved_workflow,
                "input_revision": current.input_revision,
                "input_fingerprint": current.input_fingerprint,
                "adapter_name": current.adapter_name,
                "adapter_version": current.adapter_version,
                "output_revision": validated.output_revision,
                "output_fingerprint": validated.output_fingerprint,
                "outputs": dict(published_outputs),
                "validation_proof": dict(validated.proof),
            }

            def mutate_render(
                value: RenderManifest, _operation_id: str
            ) -> RenderManifest:
                if self._current_input_fingerprint(current) != current.input_fingerprint:
                    raise _RenderInputStaleDuringPublication
                retained = tuple(
                    item
                    for item in value.clip_renders
                    if item.get("render_id") != render_state["render_id"]
                )
                return replace(
                    value,
                    clip_renders=(*retained, render_state),
                    updated_at=self.now(),
                )

            try:
                publication = publish_manifests(
                    (
                        ManifestMutation(
                            repository=self.repositories.jobs,
                            project_id=current.project_id,
                            expected_revision=jobs_manifest.revision,
                            mutate=mutate_jobs,
                        ),
                        ManifestMutation(
                            repository=self.repositories.render,
                            project_id=current.project_id,
                            expected_revision=render_manifest.revision,
                            mutate=mutate_render,
                        ),
                    )
                )
                persisted_jobs = next(
                    item
                    for item in publication.manifests
                    if isinstance(item, JobsManifest)
                )
                persisted = QueueJob.from_dict(
                    next(
                        item
                        for item in persisted_jobs.jobs
                        if item.get("job_id") == current.job_id
                    )
                )
                committed = self.queue.commit_prepared_candidate(
                    current.job_id,
                    candidate=persisted,
                    **kwargs,
                )
                self.queue.acknowledge_publication(current.project_id)
                return committed
            except _RenderInputStaleDuringPublication:
                raise
            except Exception as exc:
                raise _RenderPublicationPending(
                    current.project_id, current.job_id, exc
                ) from exc
        except _RenderPublicationPending:
            raise
        except _RenderInputStaleDuringPublication:
            finished = self.queue.mark_stale_input(current.job_id, **kwargs)
            self._publish_queue_locked(current.project_id)
            return finished
        except Exception as exc:
            failed = self.queue.mark_failed(
                current.job_id,
                f"render output publication failed: {exc}",
                **kwargs,
            )
            self._publish_queue_locked(current.project_id)
            return failed

    def _recover_render_publication(
        self, pending: _RenderPublicationPending
    ) -> QueueJob:
        from .recovery import reconcile_project

        before_reconcile = self.queue.get(pending.job_id)
        input_was_current = (
            self._current_input_fingerprint(before_reconcile)
            == before_reconcile.input_fingerprint
        )
        reconcile_project(pending.project_id, repositories=self.repositories)
        with self._state_guard(pending.project_id):
            manifest = self.repositories.jobs.load(pending.project_id)
            persisted = next(
                (
                    QueueJob.from_dict(item)
                    for item in manifest.jobs
                    if item.get("job_id") == pending.job_id
                ),
                None,
            )
            current = self.queue.get(pending.job_id)
            attempt = current.attempts[-1]
            claim_token = attempt.worker_claim_token
            if (
                persisted is not None
                and persisted.status == "success"
                and input_was_current
                and self._validate_persisted_render_success(persisted)
            ):
                if current.status == "validating":
                    committed = self.queue.commit_prepared_candidate(
                        pending.job_id,
                        candidate=persisted,
                        attempt_number=attempt.number,
                        claim_token=claim_token,
                    )
                else:
                    committed = persisted
                self.queue.acknowledge_publication(pending.project_id)
                return committed
            if persisted is not None and persisted.status == "success":
                downgraded_manifest, downgraded_ids = (
                    self._downgrade_restored_render_jobs_locked(
                        pending.project_id,
                        manifest,
                        currentness_overrides={pending.job_id: input_was_current},
                    )
                )
                if pending.job_id in downgraded_ids:
                    downgraded = QueueJob.from_dict(
                        next(
                            item
                            for item in downgraded_manifest.jobs
                            if item.get("job_id") == pending.job_id
                        )
                    )
                    committed = self.queue.commit_recovered_terminal_candidate(
                        pending.job_id,
                        candidate=downgraded,
                        attempt_number=attempt.number,
                        claim_token=claim_token,
                    )
                    self.queue.acknowledge_publication(pending.project_id)
                    return committed
            error = (
                "render publication failed before durable activation: "
                f"{pending.cause}"
            )
            failed = self.queue.mark_failed(
                pending.job_id,
                error,
                attempt_number=attempt.number,
                claim_token=claim_token,
            )
            self._publish_queue_locked(pending.project_id)
            return failed

    def _finish_analysis_job_locked(
        self,
        current: QueueJob,
        result: AdapterResult,
        *,
        lease: Mapping[str, object],
    ) -> QueueJob:
        job_id = current.job_id
        project_id = current.project_id
        attempt_number = int(lease["attempt_number"])
        claim_token = str(lease["claim_token"])
        kwargs = {
            "attempt_number": attempt_number,
            "claim_token": claim_token,
        }
        authoritative = self._current_input_fingerprint(current)
        if authoritative != current.input_fingerprint:
            finished = self.queue.mark_stale_input(job_id, **kwargs)
            self._record_analysis_job_state_locked(
                current, "stale_input", error=None
            )
            self._publish_queue_locked(project_id)
            return finished
        if result.status != "success":
            error = result.error or "analysis adapter execution failed"
            finished = self.queue.mark_failed(job_id, error, **kwargs)
            self._record_analysis_job_state_locked(current, "failed", error=error)
            self._publish_queue_locked(project_id)
            return finished
        if result.output_revision is None or result.output_fingerprint is None:
            error = "analysis adapter returned an unvalidated output identity"
            finished = self.queue.mark_failed(job_id, error, **kwargs)
            self._record_analysis_job_state_locked(current, "failed", error=error)
            self._publish_queue_locked(project_id)
            return finished
        if current.status == "running":
            self.queue.mark_validating(job_id, **kwargs)
        try:
            validate_result_path(current, result)
            if current.job_type == "video_analysis":
                candidate = self.queue.prepare_success_candidate(
                    job_id,
                    output_revision=result.output_revision,
                    output_fingerprint=result.output_fingerprint,
                    output_validated=True,
                    published_outputs=result.outputs,
                    **kwargs,
                )
                try:
                    return self._publish_analysis_completion_locked(
                        current, result, candidate=candidate, lease=kwargs
                    )
                except Exception as exc:
                    raise _AnalysisPublicationPending(
                        project_id, job_id, exc
                    ) from exc
            finished = self.queue.mark_success(
                job_id,
                output_revision=result.output_revision,
                output_fingerprint=result.output_fingerprint,
                output_validated=True,
                published_outputs=result.outputs,
                **kwargs,
            )
            self._record_analysis_job_state_locked(
                current, "running", error=None
            )
        except Exception as exc:
            if isinstance(exc, _AnalysisPublicationPending):
                raise
            error = f"analysis output publication failed: {exc}"
            finished = self.queue.mark_failed(job_id, error, **kwargs)
            self._record_analysis_job_state_locked(current, "failed", error=error)
        self._publish_queue_locked(project_id)
        return finished

    def _validate_render_result(
        self, job: QueueJob, result: AdapterResult
    ) -> _ValidatedRenderBundle:
        if (
            not result.output_revision
            or not is_safe_stable_id(result.output_revision)
            or not job.attempts
        ):
            raise ValueError("render adapter returned no safe output revision")
        attempt = Path(job.attempts[-1].directory).resolve(strict=True)
        raw_video = result.outputs.get("video")
        raw_map = result.outputs.get("frame_map")
        if not isinstance(raw_video, str) or not isinstance(raw_map, str):
            raise ValueError("render adapter outputs are incomplete")
        raw_video_path = Path(raw_video)
        raw_frame_map_path = Path(raw_map)
        if raw_video_path.is_symlink() or raw_frame_map_path.is_symlink():
            raise ValueError("render adapter output path is unsafe")
        video = raw_video_path.resolve(strict=True)
        frame_map_path = raw_frame_map_path.resolve(strict=True)
        video.relative_to(attempt)
        frame_map_path.relative_to(attempt)
        if not video.is_file() or not frame_map_path.is_file():
            raise ValueError("render adapter outputs are missing")
        return self._validate_render_files(
            job,
            output_revision=result.output_revision,
            video_path=video,
            frame_map_path=frame_map_path,
        )

    def _validate_render_files(
        self,
        job: QueueJob,
        *,
        output_revision: str,
        video_path: Path,
        frame_map_path: Path,
    ) -> _ValidatedRenderBundle:
        clips = self.repositories.clips.load(job.project_id)
        clip = next(item for item in clips.clips if item.clip_id == job.clip_id)
        source_map_path = _clip_frame_map_path(clip)
        if source_map_path is None:
            raise ValueError("authoritative source frame map is missing")
        source_frames = _load_authoritative_source_frames(clip, source_map_path)
        time_base = _fraction_time_base(clip)
        project = self.repositories.project.load(job.project_id)
        media_binding = _project_media_binding(project)
        if media_binding is None:
            raise ValueError("project media specification is missing")
        _, media_spec = media_binding
        frame_map_payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
        if not isinstance(frame_map_payload, Mapping):
            raise ValueError("render frame map must be an object")
        probed = self.media_probe(video_path)
        media_proof = validate_rendered_media(
            probed,
            frame_map_payload,
            expected_source_frames=source_frames,
            expected_source_time_base=time_base,
        )
        compatibility = media_compatibility(probed.video, media_spec)
        if not compatibility.compatible:
            raise ValueError(
                "rendered video differs from project media specification: "
                + ", ".join(compatibility.differences)
            )
        expected_time_base = {
            "numerator": time_base.numerator,
            "denominator": time_base.denominator,
        }
        video_hash = sha256(video_path.read_bytes()).hexdigest()
        map_hash = sha256(frame_map_path.read_bytes()).hexdigest()
        proof = {
            "rendered_frame_count": media_proof.rendered_frame_count,
            "output_pts": list(media_proof.output_pts),
            "source_ordinals": [frame.ordinal for frame in source_frames],
            "source_pts": list(media_proof.source_pts),
            "source_time_base": expected_time_base,
            "project_media_spec": media_spec.to_dict(),
            "video_sha256": video_hash,
            "frame_map_sha256": map_hash,
        }
        proof_fingerprint = sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return _ValidatedRenderBundle(
            output_revision=output_revision,
            output_fingerprint=proof_fingerprint,
            video_path=video_path,
            frame_map_path=frame_map_path,
            frame_map=dict(frame_map_payload),
            proof=proof,
            source_frames=source_frames,
            source_time_base=time_base,
            media_spec=media_spec,
        )

    def _publish_render_artifacts(
        self,
        job: QueueJob,
        validated: _ValidatedRenderBundle,
    ) -> Mapping[str, str]:
        revision = validated.output_revision
        target = (
            self.projects_root
            / job.project_id
            / "render_outputs"
            / job.clip_id
            / revision
        )
        manifest_name = "render_output_manifest.json"
        if target.exists():
            return self._validate_existing_render_publication(
                target, job=job, validated=validated
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{revision}-", dir=target.parent)
        )
        try:
            destination_video = temporary / "rendered.mp4"
            destination_map = temporary / "render_frame_map.json"
            shutil.copy2(validated.video_path, destination_video)
            shutil.copy2(validated.frame_map_path, destination_map)
            staged = self._validate_render_files(
                job,
                output_revision=revision,
                video_path=destination_video,
                frame_map_path=destination_map,
            )
            if (
                staged.output_fingerprint != validated.output_fingerprint
                or staged.proof != validated.proof
            ):
                raise ValueError("staged render differs from validated attempt output")
            manifest_payload = {
                "schema_version": 1,
                "project_id": job.project_id,
                "clip_id": job.clip_id,
                "job_id": job.job_id,
                "input_revision": job.input_revision,
                "input_fingerprint": job.input_fingerprint,
                "output_revision": revision,
                "output_fingerprint": validated.output_fingerprint,
                "adapter_name": job.adapter_name,
                "adapter_version": job.adapter_version,
                "validation_proof": dict(validated.proof),
            }
            manifest_path = temporary / manifest_name
            manifest_path.write_text(
                json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            for path in (destination_video, destination_map, manifest_path):
                _fsync_regular_file(path)
            os.replace(temporary, target)
            _fsync_parent_directory(target.parent)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
        return {
            "video": str(target / "rendered.mp4"),
            "frame_map": str(target / "render_frame_map.json"),
            "manifest": str(target / manifest_name),
        }

    def _validate_existing_render_publication(
        self,
        target: Path,
        *,
        job: QueueJob,
        validated: _ValidatedRenderBundle,
    ) -> Mapping[str, str]:
        paths = _validate_existing_render_publication(
            target,
            job=job,
            output_revision=validated.output_revision,
            output_fingerprint=validated.output_fingerprint,
            proof=validated.proof,
        )
        observed = self._validate_render_files(
            job,
            output_revision=validated.output_revision,
            video_path=Path(paths["video"]),
            frame_map_path=Path(paths["frame_map"]),
        )
        if (
            observed.output_fingerprint != validated.output_fingerprint
            or observed.proof != validated.proof
        ):
            raise ValueError("immutable render revision differs from validated output")
        return paths

    def _validate_persisted_render_success(self, job: QueueJob) -> bool:
        if (
            job.status != "success"
            or not job.output_validated
            or not job.output_revision
            or not is_safe_stable_id(job.output_revision)
            or not job.output_fingerprint
            or not isinstance(job.validation_proof, Mapping)
        ):
            return False
        if (
            not job.publication_operation_id
            or job.operation_id != job.publication_operation_id
        ):
            return False
        target = (
            self.projects_root
            / job.project_id
            / "render_outputs"
            / job.clip_id
            / job.output_revision
        )
        try:
            paths = {
                "video": target / "rendered.mp4",
                "frame_map": target / "render_frame_map.json",
                "manifest": target / "render_output_manifest.json",
            }
            if self._exact_render_owner_record(job) is None:
                return False
            observed = self._validate_render_files(
                job,
                output_revision=job.output_revision,
                video_path=paths["video"],
                frame_map_path=paths["frame_map"],
            )
            if (
                observed.output_fingerprint != job.output_fingerprint
                or observed.proof != job.validation_proof
            ):
                return False
            self._validate_existing_render_publication(
                target, job=job, validated=observed
            )
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _validate_persisted_render_artifact_identity(self, job: QueueJob) -> bool:
        if (
            not job.output_revision
            or not is_safe_stable_id(job.output_revision)
            or not job.output_fingerprint
            or not isinstance(job.validation_proof, Mapping)
        ):
            return False
        target = (
            self.projects_root
            / job.project_id
            / "render_outputs"
            / job.clip_id
            / job.output_revision
        )
        try:
            _validate_existing_render_publication(
                target,
                job=job,
                output_revision=job.output_revision,
                output_fingerprint=job.output_fingerprint,
                proof=job.validation_proof,
            )
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _exact_render_owner_record(
        self, job: QueueJob
    ) -> Mapping[str, object] | None:
        if (
            not job.output_revision
            or not is_safe_stable_id(job.output_revision)
            or not isinstance(job.validation_proof, Mapping)
            or not job.publication_operation_id
            or job.operation_id != job.publication_operation_id
        ):
            return None
        target = (
            self.projects_root
            / job.project_id
            / "render_outputs"
            / job.clip_id
            / job.output_revision
        )
        expected_paths = {
            "video": str(target / "rendered.mp4"),
            "frame_map": str(target / "render_frame_map.json"),
            "manifest": str(target / "render_output_manifest.json"),
        }
        if dict(job.published_outputs) != expected_paths:
            return None
        render_id = f"{job.clip_id}:{job.output_revision}"
        records = tuple(
            item
            for item in self.repositories.render.load(job.project_id).clip_renders
            if item.get("render_id") == render_id
        )
        if len(records) != 1:
            return None
        clip = next(
            (
                item
                for item in self.repositories.clips.load(job.project_id).clips
                if item.clip_id == job.clip_id
            ),
            None,
        )
        if clip is None:
            return None
        expected_record = {
            "render_id": render_id,
            "project_id": job.project_id,
            "clip_id": job.clip_id,
            "job_id": job.job_id,
            "status": "success",
            "workflow": clip.resolved_workflow,
            "input_revision": job.input_revision,
            "input_fingerprint": job.input_fingerprint,
            "adapter_name": job.adapter_name,
            "adapter_version": job.adapter_version,
            "output_revision": job.output_revision,
            "output_fingerprint": job.output_fingerprint,
            "outputs": expected_paths,
            "validation_proof": dict(job.validation_proof),
            "operation_id": job.publication_operation_id,
        }
        record = records[0]
        if set(record) != set(expected_record) or any(
            record.get(key) != value for key, value in expected_record.items()
        ):
            return None
        return record

    def _recover_analysis_publication(
        self, pending: _AnalysisPublicationPending
    ) -> QueueJob:
        from .recovery import reconcile_project

        reconcile_project(pending.project_id, repositories=self.repositories)
        with self._state_guard(pending.project_id):
            manifest = self.repositories.jobs.load(pending.project_id)
            persisted = next(
                (
                    QueueJob.from_dict(item)
                    for item in manifest.jobs
                    if item.get("job_id") == pending.job_id
                ),
                None,
            )
            current = self.queue.get(pending.job_id)
            attempt = current.attempts[-1]
            claim_token = attempt.worker_claim_token
            if persisted is not None and persisted.status == "success":
                if current.status == "validating":
                    committed = self.queue.commit_prepared_candidate(
                        pending.job_id,
                        candidate=persisted,
                        attempt_number=attempt.number,
                        claim_token=claim_token,
                    )
                else:
                    committed = persisted
                self._sync_analysis_state_from_queue_locked(pending.project_id)
                self.queue.acknowledge_publication(pending.project_id)
                return committed
            error = f"analysis publication failed before durable activation: {pending.cause}"
            failed = self.queue.mark_failed(
                pending.job_id,
                error,
                attempt_number=attempt.number,
                claim_token=claim_token,
            )
            self._record_analysis_job_state_locked(
                current, "failed", error=error
            )
            self._publish_queue_locked(pending.project_id)
            return failed

    def _publish_analysis_completion_locked(
        self,
        video_job: QueueJob,
        result: AdapterResult,
        *,
        candidate: QueueJob,
        lease: Mapping[str, object],
    ) -> QueueJob:
        if len(video_job.depends_on_job_ids) != 1:
            raise ValueError("video analysis requires exactly one CAD dependency")
        dependency = self.queue.get(video_job.depends_on_job_ids[0])
        cad_dataset = validate_dependency_output(dependency, video_job)
        analysis_output = validate_result_path(video_job, result)
        if dependency.output_fingerprint is None or result.output_fingerprint is None:
            raise ValueError("analysis content fingerprints are missing")
        artifacts = self.analysis_publisher.publish(
            project_id=video_job.project_id,
            cad_source=cad_dataset,
            cad_fingerprint=dependency.output_fingerprint,
            analysis_source=analysis_output,
            analysis_fingerprint=result.output_fingerprint,
        )
        revision = str(result.output_revision)
        project = self.repositories.project.load(video_job.project_id)
        video_asset = project.source_assets.get("video")
        video_path_value = (
            video_asset.get("path") if isinstance(video_asset, Mapping) else None
        )
        if not isinstance(video_path_value, str) or not video_path_value:
            raise ValueError("project source video is unavailable")
        media_spec = self.project_media_spec_probe(Path(video_path_value))
        media_spec_revision = _media_spec_revision(media_spec)
        active_clips = self.repositories.clips.load(video_job.project_id)
        jobs_manifest = self.repositories.jobs.load(video_job.project_id)
        input_snapshot = _analysis_input_snapshot(
            project.source_assets,
            request_key=video_job.input_revision,
            cad_dataset_id=artifacts.cad_dataset_id,
            cad_dataset_path=artifacts.cad_dataset_path,
            analysis_artifact_id=artifacts.analysis_artifact_id,
            analysis_artifact_path=artifacts.analysis_artifact_path,
        )
        clip_payload = json.loads(
            (analysis_output / "clip_manifest.json").read_text(encoding="utf-8")
        )
        clips = tuple(
            ClipDefinition.from_analysis(
                {**item, "input_snapshot": input_snapshot},
                generated_display_name=(
                    f"场景 {int(item.get('scene_index', 1)):02d} · "
                    f"第 {int(item.get('segment_index', 1))} 段"
                ),
            )
            for item in clip_payload["clips"]
        )
        if any(clip.analysis_revision != revision for clip in clips):
            raise ValueError("clip revision does not match the analysis output")
        request_key = video_job.input_revision

        def mutate_project(
            value: ProjectManifest, operation_id: str
        ) -> ProjectManifest:
            state = value.source_assets.get("_analysis")
            if not isinstance(state, Mapping) or state.get("request_key") != request_key:
                raise RevisionConflict(
                    project_id=value.project_id,
                    expected_revision=value.revision,
                    current_revision=value.revision,
                )
            assets = dict(value.source_assets)
            analysis = dict(state)
            analysis.update(
                {
                    "status": "success",
                    "analysis_revision": revision,
                    "input_snapshot": input_snapshot,
                    "analysis_artifact_id": artifacts.analysis_artifact_id,
                    "analysis_artifact_path": str(artifacts.analysis_artifact_path),
                    "error": None,
                }
            )
            assets["_analysis"] = analysis
            revisions = dict(assets.get("_analysis_revisions", {}))
            revisions[revision] = {
                "input_snapshot": input_snapshot,
                "analysis_artifact_id": artifacts.analysis_artifact_id,
                "analysis_artifact_path": str(artifacts.analysis_artifact_path),
            }
            assets["_analysis_revisions"] = revisions
            return replace(
                register_analysis_revision(value, revision, operation_id=operation_id),
                updated_at=self.now(),
                source_assets=assets,
                media_spec_revision=media_spec_revision,
                media_spec=media_spec.to_dict(),
                project_state=(
                    "ready"
                    if value.active_analysis_revision is None
                    else "analysis_candidate_ready"
                ),
            )

        def mutate_jobs(value: JobsManifest, operation_id: str) -> JobsManifest:
            if not any(item.get("job_id") == video_job.job_id for item in value.jobs):
                raise ValueError("video analysis job is missing from jobs manifest")
            return replace(
                value,
                updated_at=self.now(),
                jobs=tuple(
                    replace(
                        candidate,
                        operation_id=operation_id,
                        publication_operation_id=operation_id,
                    ).to_dict()
                    if item.get("job_id") == video_job.job_id
                    else dict(item)
                    for item in value.jobs
                ),
            )

        mutations = [
            ManifestMutation(
                repository=self.repositories.project,
                project_id=video_job.project_id,
                expected_revision=project.revision,
                mutate=mutate_project,
            ),
            ManifestMutation(
                repository=self.repositories.jobs,
                project_id=video_job.project_id,
                expected_revision=jobs_manifest.revision,
                mutate=mutate_jobs,
            ),
        ]
        if project.active_analysis_revision is None:
            mutations.append(
                ManifestMutation(
                    repository=self.repositories.clips,
                    project_id=video_job.project_id,
                    expected_revision=active_clips.revision,
                    mutate=lambda value, _operation_id: replace(
                        value,
                        updated_at=self.now(),
                        analysis_revision=revision,
                        clips=clips,
                    ),
                )
            )
        publication = publish_manifests(mutations)
        persisted_jobs = next(
            item
            for item in publication.manifests
            if isinstance(item, JobsManifest)
        )
        persisted = QueueJob.from_dict(
            next(
                item
                for item in persisted_jobs.jobs
                if item.get("job_id") == video_job.job_id
            )
        )
        committed = self.queue.commit_prepared_candidate(
            video_job.job_id,
            candidate=persisted,
            attempt_number=int(lease["attempt_number"]),
            claim_token=str(lease["claim_token"]),
        )
        self.queue.acknowledge_publication(video_job.project_id)
        return committed

    def _record_analysis_job_state_locked(
        self, job: QueueJob, status: str, *, error: str | None
    ) -> None:
        current = self.repositories.project.load(job.project_id)
        state = current.source_assets.get("_analysis")
        if not isinstance(state, Mapping) or state.get("request_key") != job.input_revision:
            return
        assets = dict(current.source_assets)
        analysis = dict(state)
        analysis.update({"status": status, "error": error})
        assets["_analysis"] = analysis
        project_state = {
            "failed": "analysis_failed",
            "interrupted": "analysis_interrupted",
            "cancelled": "analysis_cancelled",
            "stale_input": "analysis_superseded",
            "superseded": "analysis_superseded",
        }.get(status, "analyzing")
        self.repositories.project.update(
            job.project_id,
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                updated_at=self.now(),
                source_assets=assets,
                project_state=project_state,
            ),
        )

    def cancel_job(
        self,
        project_id: str,
        job_id: str,
        *,
        expected_jobs_revision: int | None = None,
    ) -> QueueJob:
        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            reservation = self.queue.begin_cancel(job_id)
            if reservation is None:
                return current
            try:
                self._publish_queue_locked(project_id)
            except Exception:
                persisted = self.repositories.jobs.load(project_id)
                stored = next(
                    (item for item in persisted.jobs if item["job_id"] == job_id),
                    None,
                )
                if stored is None or stored["status"] != "cancelling":
                    self.queue.abort_cancel(reservation)
                    raise
        error: Exception | None = None
        try:
            self.queue.terminate_cancel_reservation(reservation)
        except Exception as exc:
            error = exc
        with self._state_guard(project_id):
            cancelled = self.queue.complete_cancel(reservation, error=error)
            if current.job_type in {"cad_analysis", "video_analysis"}:
                self._record_analysis_job_state_locked(
                    current, cancelled.status, error=cancelled.error
                )
            self._publish_queue_locked(project_id)
            return cancelled

    def job_runtime(
        self,
        project_id: str,
        job_id: str,
        *,
        tail: int = 30,
    ) -> dict[str, object]:
        """Read live adapter-owned status without making it project state."""
        job = self.queue.get(job_id)
        if job.project_id != project_id:
            raise KeyError(f"job {job_id} does not belong to {project_id}")
        if not job.attempts:
            return {
                "project_id": project_id,
                "job_id": job_id,
                "status": job.status,
                "stage": job.stage,
                "workflow_status": None,
                "lines": [],
            }

        attempt = job.attempts[-1]
        attempt_dir = Path(attempt.directory).resolve(strict=False)
        expected_dir = self._attempt_directory(
            project_id, job_id, attempt.number
        ).resolve(strict=False)
        if attempt_dir != expected_dir:
            raise ValueError("job attempt directory is outside the project job root")

        status_path = attempt_dir / project_id / job.clip_id / "job_status.json"
        workflow_status: Mapping[str, object] | None = None
        if status_path.is_file():
            try:
                candidate = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, Mapping):
                workflow_status = candidate

        log_path = Path(attempt.log_path or (attempt_dir / "adapter.log")).resolve(
            strict=False
        )
        try:
            log_path.relative_to(attempt_dir)
        except ValueError as exc:
            raise ValueError("job log path is outside the current attempt") from exc
        count = max(1, min(int(tail), 2000))
        lines = (
            read_workflow_log_text(log_path).splitlines()[-count:]
            if log_path.is_file()
            else []
        )
        return {
            "project_id": project_id,
            "job_id": job_id,
            "status": job.status,
            "stage": job.stage,
            "workflow_status": workflow_status,
            "lines": lines,
        }

    def prepare_job_execution(
        self,
        project_id: str,
        job_id: str,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> JobExecutionPlan:
        with self._state_guard(project_id):
            job = self.queue.get(job_id)
            if job.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            if self._current_input_fingerprint(job) != job.input_fingerprint:
                self.queue.mark_superseded(
                    job_id,
                    attempt_number=attempt_number,
                    claim_token=claim_token,
                )
                if job.job_type in {"cad_analysis", "video_analysis"}:
                    self._record_analysis_job_state_locked(
                        job, "superseded", error=None
                    )
                self._publish_queue_locked(project_id)
                raise RuntimeError("job input changed before execution")
            return self._build_job_execution_plan_locked(job)

    def _build_job_execution_plan_locked(
        self, job: QueueJob
    ) -> JobExecutionPlan:
        project_id = job.project_id
        if job.job_type == "clip_export":
            return self._prepare_clip_export(job)
        if job.job_type in {"cad_analysis", "video_analysis"}:
            return self._prepare_analysis(job)
        if job.job_type == "clip_render":
            return self._prepare_clip_render(job)
        if job.job_type != "trajectory":
            raise ValueError(f"unsupported executable job type: {job.job_type}")
        clips_manifest = self.repositories.clips.load(project_id)
        project = self.repositories.project.load(project_id)
        clip = next(
            item for item in clips_manifest.clips if item.clip_id == job.clip_id
        )
        adapter = self.adapters.for_workflow(str(clip.resolved_workflow))
        video_path = _clip_output_path(clip)
        frame_map_path = _clip_frame_map_path(clip)
        if job.depends_on_job_ids:
            dependency = self.queue.get(job.depends_on_job_ids[0])
            if dependency.status != "success" or not dependency.output_validated:
                raise RuntimeError("clip export dependency is not validated")
            video_value = dependency.published_outputs.get(f"video:{clip.clip_id}")
            map_value = dependency.published_outputs.get(f"frame_map:{clip.clip_id}")
            video_path = None if video_value is None else Path(video_value)
            frame_map_path = None if map_value is None else Path(map_value)
        if video_path is None:
            raise FileNotFoundError("physical clip MP4 is unavailable")
        time_base = _fraction_time_base(clip)
        inputs = AdapterInputs(
            project_id=project_id,
            clip_id=clip.clip_id,
            video_path=video_path,
            srt_path=_clip_asset_path(clip, project.source_assets, "srt"),
            attempt_directory=Path(job.attempts[-1].directory),
            parameters=dict(clip.manual_definition),
            source_start_pts=int(clip.analysis["source_start_pts"]),
            source_end_pts_exclusive=int(clip.analysis["source_end_pts_exclusive"]),
            source_time_base=time_base,
            frame_map_path=frame_map_path,
        )
        prepared = adapter.prepare_inputs(inputs)
        return JobExecutionPlan(
            commands=adapter.build_commands(prepared),
            validate=lambda: adapter.validate_outputs(prepared),
        )

    def _prepare_clip_render(self, job: QueueJob) -> JobExecutionPlan:
        if (
            job.status != "running"
            or job.resource_class != "media_io"
            or job.exclusive_key != f"render:{job.project_id}:{job.clip_id}"
            or len(job.depends_on_job_ids) != 1
            or not job.attempts
        ):
            raise RuntimeError("clip render scheduling contract is invalid")
        dependency = self.queue.get(job.depends_on_job_ids[0])
        if (
            not _has_exact_success_proof(dependency)
            or not _trajectory_artifact_matches_proof(dependency)
        ):
            raise RuntimeError("clip render trajectory dependency is not validated")
        project = self.repositories.project.load(job.project_id)
        clips = self.repositories.clips.load(job.project_id)
        clip = next(
            (item for item in clips.clips if item.clip_id == job.clip_id), None
        )
        media_binding = _project_media_binding(project)
        if clip is None or media_binding is None:
            raise RuntimeError("clip render project inputs are unavailable")
        adapter = self.render_adapters.for_workflow(str(clip.resolved_workflow))
        if adapter.name != job.adapter_name or adapter.version != job.adapter_version:
            raise RuntimeError("clip render adapter identity changed")
        stored_jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(job.project_id).jobs
        )
        physical_video, frame_map = _render_physical_inputs(clip, stored_jobs)
        if physical_video is None or frame_map is None:
            raise RuntimeError("clip render physical inputs are unavailable")
        workbench = _saved_workbench_reference(clip)
        if (
            workbench is None
            or not _workbench_binds_trajectory(clip, dependency, workbench.value)
            or not _validate_workbench_immutable_output(
                self.projects_root, job.project_id, clip, workbench
            )
        ):
            raise RuntimeError("clip render workbench input is not current")
        attempt = Path(job.attempts[-1].directory).resolve(strict=True)
        expected_attempt = self._attempt_directory(
            job.project_id, job.job_id, job.attempts[-1].number
        ).resolve(strict=True)
        if attempt != expected_attempt or not attempt.is_dir():
            raise RuntimeError("clip render attempt directory identity is invalid")
        _, media_spec = media_binding
        inputs = RenderInputs(
            project_id=job.project_id,
            clip_id=clip.clip_id,
            workflow=str(clip.resolved_workflow),
            physical_video_path=physical_video.resolve(strict=True),
            authoritative_frame_map_path=frame_map.resolve(strict=True),
            authoritative_source_frames=_load_authoritative_source_frames(
                clip, frame_map
            ),
            source_time_base=_fraction_time_base(clip),
            workbench_artifact_path=_workbench_artifact_path(
                self.projects_root, job.project_id, workbench
            ),
            workbench_output_revision=str(
                workbench.value["workbench_output_revision"]
            ),
            workbench_output_fingerprint=str(
                workbench.value["workbench_output_fingerprint"]
            ),
            attempt_directory=attempt,
            project_media_spec=media_spec,
            parameters={
                **dict(clip.manual_definition),
                **_workbench_render_parameters(
                    self.storage_root, job.project_id, clip
                ),
                "trajectory_path": dependency.published_outputs["trajectory"],
            },
        )
        plan = adapter.prepare(inputs)
        return JobExecutionPlan(commands=plan.commands, validate=plan.validate)

    def prepare_source_interval_render(
        self, inputs: SourceIntervalRenderInputs
    ) -> JobExecutionPlan:
        """Build a manifest-free fallback plan for later merge orchestration."""

        plan = self.source_interval_render_adapter.prepare(inputs)
        return JobExecutionPlan(commands=plan.commands, validate=plan.validate)

    def record_job_process(
        self,
        project_id: str,
        job_id: str,
        *,
        pid: int,
        process_start_time: str,
        command_fingerprint: str,
        task_token: str,
        log_path: str,
        attempt_number: int,
        claim_token: str,
        terminate=None,
    ) -> QueueJob:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            updated = self.queue.record_process(
                job_id,
                pid=pid,
                process_start_time=process_start_time,
                command_fingerprint=command_fingerprint,
                task_token=task_token,
                log_path=log_path,
                attempt_number=attempt_number,
                claim_token=claim_token,
                terminate=terminate,
            )
            self._publish_queue_locked(project_id)
            return updated

    def update_job_progress(
        self,
        project_id: str,
        job_id: str,
        progress,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            updated = self.queue.update_progress(
                job_id,
                progress,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            if current.job_type in {"cad_analysis", "video_analysis"}:
                self._record_analysis_job_state_locked(
                    current, str(progress.stage), error=None
                )
            self._publish_queue_locked(project_id)
            return updated

    def fail_job(
        self,
        project_id: str,
        job_id: str,
        error: str,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> QueueJob:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            failed = self.queue.mark_failed(
                job_id,
                error,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )
            if current.job_type in {"cad_analysis", "video_analysis"}:
                self._record_analysis_job_state_locked(
                    current, "failed", error=error
                )
            self._publish_queue_locked(project_id)
            return failed

    def register_job_process_controller(
        self,
        project_id: str,
        job_id: str,
        terminate,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> None:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            self.queue.register_process_controller(
                job_id,
                terminate,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )

    def release_job_process_controller(
        self,
        project_id: str,
        job_id: str,
        pid: int,
        *,
        attempt_number: int,
        claim_token: str,
    ) -> None:
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            self.queue.release_process_controller(
                job_id,
                pid,
                attempt_number=attempt_number,
                claim_token=claim_token,
            )

    def retry_job(
        self,
        project_id: str,
        job_id: str,
        *,
        expected_jobs_revision: int | None = None,
    ) -> QueueJob:
        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            number = len(current.attempts) + 1
            directory = self._attempt_directory(project_id, job_id, number)
            directory.mkdir(parents=True, exist_ok=False)
            try:
                retried = self.queue.retry(
                    job_id,
                    AttemptRecord(number=number, directory=str(directory)),
                )
            except Exception:
                directory.rmdir()
                raise
            if current.job_type in {"cad_analysis", "video_analysis"}:
                self._record_analysis_job_state_locked(
                    current, "queued", error=None
                )
            self._publish_queue_locked(project_id)
            return retried

    def _require_jobs_revision_locked(
        self, project_id: str, expected_jobs_revision: int | None
    ) -> None:
        if expected_jobs_revision is None:
            return
        current = self.repositories.jobs.load(project_id)
        if current.revision != expected_jobs_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected_jobs_revision,
                current_revision=current.revision,
            )

    def restore_jobs(
        self,
        project_id: str,
        *,
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
        unverified_process_policy: str = "fail_closed",
    ) -> LocalResourceQueue:
        with self._state_guard(project_id):
            manifest = self.repositories.jobs.load(project_id)
            manifest = self._migrate_legacy_analysis_jobs_locked(
                project_id, manifest
            )
            manifest, downgraded_render_ids = self._downgrade_restored_render_jobs_locked(
                project_id, manifest
            )
            restored = self.queue.merge_restored(
                manifest.jobs,
                project_id=project_id,
                queue_order=manifest.queue_order,
                process_probe=process_probe,
                current_fingerprint_resolver=lambda job: (
                    job.input_fingerprint
                    if job.job_id in downgraded_render_ids
                    else self._current_input_fingerprint(job)
                ),
                defer_cleanup=True,
                unverified_process_policy=unverified_process_policy,
            )
            self._sync_analysis_state_from_queue_locked(project_id)
            self._publish_queue_locked(project_id)
            cleanup_reservations = self.queue.pending_restore_cleanups(project_id)
        cleanup_results: list[tuple[RestoreCleanupReservation, Exception | None]] = []
        for reservation in cleanup_reservations:
            cleanup_error: Exception | None = None
            try:
                self.queue.terminate_restore_cleanup(reservation)
            except Exception as exc:
                cleanup_error = exc
            cleanup_results.append((reservation, cleanup_error))
        if cleanup_results:
            with self._state_guard(project_id):
                for reservation, cleanup_error in cleanup_results:
                    self.queue.complete_restore_cleanup(
                        reservation,
                        error=cleanup_error,
                    )
                self._publish_queue_locked(project_id)
        return restored

    def _downgrade_restored_render_jobs_locked(
        self,
        project_id: str,
        jobs_manifest: JobsManifest,
        *,
        currentness_overrides: Mapping[str, bool] | None = None,
    ) -> tuple[JobsManifest, frozenset[str]]:
        decisions: dict[str, str] = {}
        render_ids: dict[str, str] = {}
        for item in jobs_manifest.jobs:
            job = QueueJob.from_dict(item)
            if job.job_type != "clip_render" or job.status != "success":
                continue
            if job.output_revision:
                render_ids[f"{job.clip_id}:{job.output_revision}"] = job.job_id
            try:
                owner_valid = self._exact_render_owner_record(job) is not None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                owner_valid = False
            if not owner_valid:
                decisions[job.job_id] = "failed"
                continue
            if not self._validate_persisted_render_artifact_identity(job):
                decisions[job.job_id] = "failed"
                continue
            is_current = (
                currentness_overrides[job.job_id]
                if currentness_overrides is not None
                and job.job_id in currentness_overrides
                else self._current_input_fingerprint(job) == job.input_fingerprint
            )
            if not is_current:
                decisions[job.job_id] = "superseded"
                continue
            if not self._validate_persisted_render_success(job):
                decisions[job.job_id] = "failed"
        if not decisions:
            return jobs_manifest, frozenset()
        render_manifest = self.repositories.render.load(project_id)

        def mutate_jobs(value: JobsManifest, operation_id: str) -> JobsManifest:
            changed: list[Mapping[str, object]] = []
            for payload in value.jobs:
                status = decisions.get(str(payload.get("job_id")))
                if status is None:
                    changed.append(dict(payload))
                    continue
                job = QueueJob.from_dict(payload)
                changed.append(
                    replace(
                        job,
                        status=status,
                        stage=status,
                        operation_id=operation_id,
                        publication_operation_id=operation_id,
                        output_validated=False,
                        validated_input_fingerprint=None,
                        error=(
                            "persisted render output failed restore validation"
                            if status == "failed"
                            else "persisted render input is no longer current"
                        ),
                    ).to_dict()
                )
            return replace(value, jobs=tuple(changed), updated_at=self.now())

        def mutate_render(value: RenderManifest, operation_id: str) -> RenderManifest:
            changed: list[Mapping[str, object]] = []
            for record in value.clip_renders:
                job_id = render_ids.get(str(record.get("render_id")))
                status = decisions.get(job_id or str(record.get("job_id")))
                if status is None:
                    changed.append(dict(record))
                    continue
                changed.append(
                    {
                        **record,
                        "status": (
                            "failed_validation"
                            if status == "failed"
                            else "stale_input"
                        ),
                        "operation_id": operation_id,
                        "validation_error": (
                            "persisted_output_invalid"
                            if status == "failed"
                            else "input_fingerprint_changed"
                        ),
                    }
                )
            return replace(
                value,
                clip_renders=tuple(changed),
                updated_at=self.now(),
            )

        publication = publish_manifests(
            (
                ManifestMutation(
                    repository=self.repositories.jobs,
                    project_id=project_id,
                    expected_revision=jobs_manifest.revision,
                    mutate=mutate_jobs,
                ),
                ManifestMutation(
                    repository=self.repositories.render,
                    project_id=project_id,
                    expected_revision=render_manifest.revision,
                    mutate=mutate_render,
                ),
            )
        )
        persisted = next(
            item for item in publication.manifests if isinstance(item, JobsManifest)
        )
        return persisted, frozenset(decisions)

    def _migrate_legacy_analysis_jobs_locked(
        self,
        project_id: str,
        manifest: JobsManifest,
    ) -> JobsManifest:
        jobs = tuple(QueueJob.from_dict(item) for item in manifest.jobs)
        if any(item.project_id != project_id for item in jobs):
            raise ValueError(
                "jobs manifest contains a job owned by another project"
            )
        project = self.repositories.project.load(project_id)
        state = project.source_assets.get("_analysis")
        if not isinstance(state, Mapping):
            return manifest
        request_key = str(state.get("request_key") or "")
        recorded_ids = tuple(str(item) for item in state.get("job_ids", ()))
        if not request_key or not recorded_ids:
            return manifest
        by_id = {item.job_id: item for item in jobs}
        if len(recorded_ids) != 2 or len(set(recorded_ids)) != 2:
            raise ValueError("recorded analysis DAG must contain two unique jobs")
        try:
            recorded = tuple(by_id[job_id] for job_id in recorded_ids)
        except KeyError as exc:
            raise ValueError("recorded analysis DAG job is missing") from exc
        structured = self._validated_analysis_dag_structure(
            recorded,
            project_id=project_id,
            request_key=request_key,
        )
        if structured is None:
            raise ValueError("recorded analysis DAG is structurally invalid")
        if self._validated_analysis_dag(
            structured,
            project_id=project_id,
            request_key=request_key,
            project_assets=project.source_assets,
        ) is not None:
            return manifest

        replacements: dict[str, QueueJob] = {}
        for phase, job in (("cad", structured[0]), ("video", structured[1])):
            current_contract = _analysis_job_contract(
                project_id=project_id,
                phase=phase,
                request_key=request_key,
                project_assets=project.source_assets,
            )
            for field_name, expected in current_contract.items():
                if field_name in {"input_fingerprint", "idempotency_key"}:
                    continue
                if getattr(job, field_name) != expected:
                    raise ValueError(
                        "legacy analysis job does not match canonical contract"
                    )
            legacy_payload = _legacy_analysis_identity_payload(
                phase=phase,
                request_key=request_key,
                project_assets=project.source_assets,
            )
            legacy_fingerprint = _fingerprint(legacy_payload)
            legacy_idempotency = _fingerprint(
                {**legacy_payload, "purpose": "idempotency"}
            )
            if (
                job.input_fingerprint != legacy_fingerprint
                or job.idempotency_key != legacy_idempotency
            ):
                raise ValueError(
                    "legacy analysis identity does not match exactly"
                )
            if job.status == "success":
                if not _has_exact_success_proof(job):
                    raise ValueError(
                        "legacy analysis success has no complete validation proof"
                    )
            elif (
                job.output_validated
                or job.validated_input_fingerprint is not None
            ):
                raise ValueError(
                    "non-success legacy analysis job carries validation proof"
                )
            replacements[job.job_id] = replace(
                job,
                input_fingerprint=str(current_contract["input_fingerprint"]),
                idempotency_key=str(current_contract["idempotency_key"]),
                validated_input_fingerprint=(
                    str(current_contract["input_fingerprint"])
                    if job.validated_input_fingerprint is not None
                    else None
                ),
            )

        return self.repositories.jobs.update(
            project_id,
            expected_revision=manifest.revision,
            mutate=lambda value: replace(
                value,
                operation_id=self._identity(),
                jobs=tuple(
                    replacements.get(item.job_id, item).to_dict()
                    for item in jobs
                ),
            ),
        )

    def _sync_analysis_state_from_queue_locked(self, project_id: str) -> None:
        project = self.repositories.project.load(project_id)
        state = project.source_assets.get("_analysis")
        if not isinstance(state, Mapping):
            return
        request_key = str(state.get("request_key") or "")
        job_ids = tuple(str(item) for item in state.get("job_ids", ()))
        if not request_key or not job_ids:
            return
        analysis_jobs = []
        known = {item.job_id: item for item in self.queue.jobs()}
        for job_id in job_ids:
            job = known.get(job_id)
            if (
                job is not None
                and job.project_id == project_id
                and job.input_revision == request_key
                and job.job_type in {"cad_analysis", "video_analysis"}
            ):
                analysis_jobs.append(job)
        jobs = self._validated_analysis_dag(
            tuple(analysis_jobs),
            project_id=project_id,
            request_key=request_key,
            project_assets=project.source_assets,
        )
        if jobs is None:
            status = "failed"
            error = "recorded analysis DAG is structurally invalid"
        elif all(_has_exact_success_proof(item) for item in jobs):
            status = "success"
            error = None
        elif any(
            item.status == "success" and not _has_exact_success_proof(item)
            for item in jobs
        ):
            status = "failed"
            error = "analysis success has no matching validated input proof"
        else:
            terminal_problem = next(
                (
                    item
                    for item in jobs
                    if item.status
                    in {
                        "failed",
                        "interrupted",
                        "cancelled",
                        "stale_input",
                        "superseded",
                    }
                ),
                None,
            )
            if terminal_problem is not None:
                status = terminal_problem.status
                error = terminal_problem.error
            elif any(
                item.status in {"preparing", "running", "validating"}
                for item in jobs
            ):
                status = "running"
                error = None
            else:
                status = "queued"
                error = None
        if state.get("status") == status and state.get("error") == error:
            return
        assets = dict(project.source_assets)
        updated_state = dict(state)
        updated_state.update({"status": status, "error": error})
        assets["_analysis"] = updated_state
        project_state = {
            "success": (
                "analysis_candidate_ready"
                if project.candidate_analysis_revision is not None
                else "ready"
            ),
            "failed": "analysis_failed",
            "interrupted": "analysis_interrupted",
            "cancelled": "analysis_cancelled",
            "stale_input": "analysis_superseded",
            "superseded": "analysis_superseded",
        }.get(status, "analyzing")
        self.repositories.project.update(
            project_id,
            expected_revision=project.revision,
            mutate=lambda value: replace(
                value,
                updated_at=self.now(),
                source_assets=assets,
                project_state=project_state,
            ),
        )

    def reap_adopted_jobs(self) -> tuple[str, ...]:
        with self._publication_lock:
            reaped = self.queue.poll_adopted_processes()
            project_ids = {
                self.queue.get(job_id).project_id for job_id in reaped
            } | set(self.queue.pending_publication_projects())
            failures: list[tuple[str, Exception]] = []
            for project_id in sorted(project_ids):
                try:
                    with self._state_guard(project_id):
                        self._sync_analysis_state_from_queue_locked(project_id)
                    self._publish_queue(project_id)
                except Exception as exc:
                    failures.append((project_id, exc))
            if failures:
                details = "; ".join(
                    f"{project_id}: {error}" for project_id, error in failures
                )
                raise RuntimeError(
                    f"job-state publication failed for one or more projects: {details}"
                ) from failures[0][1]
            return reaped

    def _prepare_analysis_submission(
        self,
        project_id: str,
        *,
        request_key: str,
        project_assets: Mapping[str, object],
    ) -> PreparedSubmissionBatch:
        operation_seed = self._identity()
        cad_job = self._new_analysis_job(
            project_id,
            request_key=request_key,
            phase="cad",
            operation_id=operation_seed,
            dependency_ids=(),
            project_assets=project_assets,
        )
        video_job = self._new_analysis_job(
            project_id,
            request_key=request_key,
            phase="video",
            operation_id=operation_seed,
            dependency_ids=(cad_job.job_id,),
            project_assets=project_assets,
        )
        batch = self.queue.prepare_submission_candidates((cad_job, video_job))
        for candidate in batch.new_candidates:
            Path(candidate.attempts[-1].directory).mkdir(
                parents=True, exist_ok=False
            )
        return batch

    @staticmethod
    def _validated_analysis_dag(
        jobs: Sequence[QueueJob],
        *,
        project_id: str,
        request_key: str,
        project_assets: Mapping[str, object],
    ) -> tuple[QueueJob, QueueJob] | None:
        structured = ProjectService._validated_analysis_dag_structure(
            jobs,
            project_id=project_id,
            request_key=request_key,
        )
        if structured is None:
            return None
        cad, video = structured
        for phase, job in (("cad", cad), ("video", video)):
            contract = _analysis_job_contract(
                project_id=project_id,
                request_key=request_key,
                phase=phase,
                project_assets=project_assets,
            )
            if any(
                getattr(job, field_name) != expected
                for field_name, expected in contract.items()
            ):
                return None
        return cad, video

    @staticmethod
    def _validated_analysis_dag_structure(
        jobs: Sequence[QueueJob],
        *,
        project_id: str,
        request_key: str,
    ) -> tuple[QueueJob, QueueJob] | None:
        if len(jobs) != 2 or len({item.job_id for item in jobs}) != 2:
            return None
        if any(
            item.project_id != project_id or item.input_revision != request_key
            for item in jobs
        ):
            return None
        by_type = {item.job_type: item for item in jobs}
        if set(by_type) != {"cad_analysis", "video_analysis"}:
            return None
        cad = by_type["cad_analysis"]
        video = by_type["video_analysis"]
        if cad.depends_on_job_ids or video.depends_on_job_ids != (cad.job_id,):
            return None
        return cad, video

    @staticmethod
    def _validated_analysis_descriptor(
        project_assets: Mapping[str, object],
        *,
        revision: str,
        request_key: str,
    ) -> dict[str, object] | None:
        revisions = project_assets.get("_analysis_revisions")
        if not isinstance(revisions, Mapping):
            return None
        descriptor = revisions.get(revision)
        if not isinstance(descriptor, Mapping):
            return None
        snapshot = descriptor.get("input_snapshot")
        artifact_id = descriptor.get("analysis_artifact_id")
        artifact_path = descriptor.get("analysis_artifact_path")
        if (
            not isinstance(snapshot, Mapping)
            or not isinstance(artifact_id, str)
            or not artifact_id
            or not isinstance(artifact_path, str)
            or not artifact_path
            or snapshot.get("request_key") != request_key
        ):
            return None

        for asset_name in ("video", "cad"):
            current = project_assets.get(asset_name)
            captured = snapshot.get(asset_name)
            if not isinstance(current, Mapping) or not isinstance(captured, Mapping):
                return None
            for identity_field in ("path", "sha256"):
                current_value = current.get(identity_field)
                captured_value = captured.get(identity_field)
                if (
                    not isinstance(current_value, str)
                    or not current_value
                    or not isinstance(captured_value, str)
                    or captured_value != current_value
                ):
                    return None

        current_srt = project_assets.get("srt")
        captured_srt = snapshot.get("srt")
        if current_srt is None and captured_srt is None:
            pass
        elif isinstance(current_srt, Mapping) and isinstance(captured_srt, Mapping):
            for identity_field in ("path", "sha256"):
                current_value = current_srt.get(identity_field)
                captured_value = captured_srt.get(identity_field)
                if (
                    not isinstance(current_value, str)
                    or not current_value
                    or not isinstance(captured_value, str)
                    or captured_value != current_value
                ):
                    return None
        else:
            return None

        snapshot_artifact = snapshot.get("analysis_artifact")
        if not isinstance(snapshot_artifact, Mapping):
            return None
        if (
            snapshot_artifact.get("artifact_id") != artifact_id
            or snapshot_artifact.get("path") != artifact_path
        ):
            return None
        return {
            "input_snapshot": dict(snapshot),
            "analysis_artifact_id": artifact_id,
            "analysis_artifact_path": artifact_path,
        }

    def _analysis_state_for_submission(
        self,
        *,
        batch: PreparedSubmissionBatch,
        project: ProjectManifest,
        project_assets: Mapping[str, object],
        request_key: str,
        base_state: Mapping[str, object] | None,
        operation_id: str,
    ) -> tuple[dict[str, object], str]:
        state = dict(base_state or {})
        result_fields = (
            "analysis_revision",
            "input_snapshot",
            "analysis_artifact_id",
            "analysis_artifact_path",
        )
        for field_name in result_fields:
            state.pop(field_name, None)
        state.update(
            {
                "request_key": request_key,
                "job_ids": list(batch.job_ids),
                "operation_id": operation_id,
            }
        )
        jobs = self._validated_analysis_dag(
            batch.jobs,
            project_id=project.project_id,
            request_key=request_key,
            project_assets=project_assets,
        )
        if jobs is None:
            state.update(
                {
                    "status": "failed",
                    "error": "reused analysis DAG is structurally invalid",
                }
            )
            return state, "analysis_failed"
        video = jobs[1]
        fully_validated = (
            all(_has_exact_success_proof(item) for item in jobs)
            and video.output_revision is not None
        )
        if fully_validated:
            revision = str(video.output_revision)
            descriptor = self._validated_analysis_descriptor(
                project_assets,
                revision=revision,
                request_key=request_key,
            )
            if descriptor is None:
                state.update(
                    {
                        "status": "failed",
                        "error": (
                            "reused successful analysis has no complete immutable descriptor"
                        ),
                    }
                )
                return state, "analysis_failed"
            state.update(descriptor)
            state.update(
                {
                    "status": "success",
                    "analysis_revision": revision,
                    "error": None,
                }
            )
            project_state = (
                "analysis_candidate_ready"
                if project.active_analysis_revision not in {None, revision}
                else "ready"
            )
            return state, project_state

        invalid_success = next(
            (
                item
                for item in jobs
                if item.status == "success"
                and not _has_exact_success_proof(item)
            ),
            None,
        )
        if invalid_success is not None:
            state.update(
                {
                    "status": "failed",
                    "error": (
                        "reused analysis success has no matching validated input proof"
                    ),
                }
            )
            return state, "analysis_failed"

        terminal_states = {
            "failed": "analysis_failed",
            "interrupted": "analysis_interrupted",
            "cancelled": "analysis_cancelled",
            "stale_input": "analysis_superseded",
            "superseded": "analysis_superseded",
        }
        terminal = next(
            (item for item in jobs if item.status in terminal_states),
            None,
        )
        if terminal is not None:
            state.update(
                {"status": terminal.status, "error": terminal.error}
            )
            return state, terminal_states[terminal.status]
        if any(
            item.status in {"preparing", "running", "validating"}
            for item in jobs
        ):
            state.update({"status": "running", "error": None})
            return state, "analyzing"
        if any(item.status == "queued" for item in jobs):
            state.update({"status": "queued", "error": None})
            return state, "analyzing"
        state.update(
            {
                "status": "failed",
                "error": "reused analysis DAG has no schedulable or validated state",
            }
        )
        return state, "analysis_failed"

    @staticmethod
    def _analysis_submission_payload(
        *,
        by_id: Mapping[str, QueueJob],
        order: Sequence[str],
        batch: PreparedSubmissionBatch,
        operation_id: str,
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(
            (
                replace(
                    by_id[job_id],
                    operation_id=operation_id,
                    submission_operation_id=operation_id,
                ).to_dict()
                if job_id in batch.new_job_ids
                else by_id[job_id].to_dict()
            )
            for job_id in order
        )

    def _commit_analysis_submission(
        self,
        batch: PreparedSubmissionBatch,
        persisted_by_id: Mapping[str, QueueJob],
    ) -> tuple[QueueJob, ...]:
        if batch.new_candidates:
            self.queue.commit_submission_candidates(
                tuple(
                    persisted_by_id[candidate.job_id]
                    for candidate in batch.new_candidates
                )
            )
        return tuple(self.queue.get(job_id) for job_id in batch.job_ids)

    def _new_analysis_job(
        self,
        project_id: str,
        *,
        request_key: str,
        phase: str,
        operation_id: str,
        dependency_ids: tuple[str, ...],
        project_assets: Mapping[str, object],
    ) -> QueueJob:
        if phase not in {"cad", "video"}:
            raise ValueError(f"unsupported analysis phase: {phase}")
        contract = _analysis_job_contract(
            project_id=project_id,
            request_key=request_key,
            phase=phase,
            project_assets=project_assets,
        )
        job_id = self._identity()
        attempt_dir = self._attempt_directory(project_id, job_id, 1)
        return QueueJob(
            job_id=job_id,
            status="queued",
            stage="queued",
            depends_on_job_ids=dependency_ids,
            output_revision=None,
            operation_id=operation_id,
            attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
            **contract,
        )

    def _new_job(
        self,
        project_id: str,
        clip: ClipDefinition,
        *,
        job_type: str,
        resource_class: str,
        adapter_name: str,
        adapter_version: str,
        exclusive_key: str,
        dependency_ids: tuple[str, ...],
        project_assets: Mapping[str, object],
        project_revision: int,
        clips_revision: int,
    ) -> QueueJob:
        identity_payload = _job_identity_payload(
            job_type=job_type,
            clip=clip,
            project_assets=project_assets,
            project_revision=project_revision,
            clips_revision=clips_revision,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
        )
        input_fingerprint = _fingerprint(identity_payload)
        idempotency_key = _fingerprint({**identity_payload, "purpose": "idempotency"})
        job_id = self._identity()
        operation_id = self._identity()
        attempt_dir = self._attempt_directory(project_id, job_id, 1)
        return QueueJob(
            job_id=job_id,
            project_id=project_id,
            clip_id=clip.clip_id,
            job_type=job_type,
            resource_class=resource_class,
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=dependency_ids,
            exclusive_key=exclusive_key,
            idempotency_key=idempotency_key,
            input_revision=clip.analysis_revision,
            input_fingerprint=input_fingerprint,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            output_revision=None,
            operation_id=operation_id,
            attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
        )

    def _new_export_job(
        self,
        project_id: str,
        clip: ClipDefinition,
        *,
        project_assets: Mapping[str, object],
        project_revision: int,
        clips_revision: int,
    ) -> QueueJob:
        identity_payload = _export_identity_payload(
            clip=clip,
            project_assets=project_assets,
            project_revision=project_revision,
            clips_revision=clips_revision,
        )
        input_fingerprint = _fingerprint(identity_payload)
        job_id = self._identity()
        attempt_dir = self._attempt_directory(project_id, job_id, 1)
        return QueueJob(
            job_id=job_id,
            project_id=project_id,
            clip_id=clip.clip_id,
            job_type="clip_export",
            resource_class="media_io",
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=(),
            exclusive_key=f"export:{project_id}",
            idempotency_key=_fingerprint(
                {**identity_payload, "purpose": "idempotency"}
            ),
            input_revision=clip.analysis_revision,
            input_fingerprint=input_fingerprint,
            adapter_name="clip_export",
            adapter_version="1",
            output_revision=None,
            operation_id=self._identity(),
            attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
        )

    def _new_render_job(
        self,
        project_id: str,
        clip: ClipDefinition,
        *,
        trajectory: QueueJob,
        workbench: StateReference,
        adapter_name: str,
        adapter_version: str,
        project_revision: int,
        clips_revision: int,
        media_spec: ProjectMediaSpec,
        media_spec_revision: str,
        physical_video_path: Path | None = None,
        physical_frame_map_path: Path | None = None,
    ) -> QueueJob:
        identity_payload = _render_identity_payload(
            clip=clip,
            project_revision=project_revision,
            clips_revision=clips_revision,
            trajectory=trajectory,
            workbench=workbench,
            media_spec=media_spec,
            media_spec_revision=media_spec_revision,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            physical_video_path=physical_video_path,
            physical_frame_map_path=physical_frame_map_path,
        )
        input_fingerprint = _fingerprint(identity_payload)
        revision_fingerprint = _fingerprint(
            {
                "analysis_revision": clip.analysis_revision,
                "workbench_output_revision": str(
                    workbench.value["workbench_output_revision"]
                ),
                "media_spec_revision": media_spec_revision,
            }
        )
        job_id = self._identity()
        attempt_dir = self._attempt_directory(project_id, job_id, 1)
        return QueueJob(
            job_id=job_id,
            project_id=project_id,
            clip_id=clip.clip_id,
            job_type="clip_render",
            resource_class="media_io",
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=(trajectory.job_id,),
            exclusive_key=f"render:{project_id}:{clip.clip_id}",
            idempotency_key=_fingerprint(
                {**identity_payload, "purpose": "idempotency"}
            ),
            input_revision=revision_fingerprint,
            input_fingerprint=input_fingerprint,
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            output_revision=None,
            operation_id=self._identity(),
            attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
        )

    def _current_input_fingerprint(self, job: QueueJob) -> str | None:
        project = self.repositories.project.load(job.project_id)
        if job.job_type in {"cad_analysis", "video_analysis"}:
            analysis = project.source_assets.get("_analysis")
            if not isinstance(analysis, Mapping):
                return None
            request_key = str(analysis.get("request_key") or "")
            if request_key != job.input_revision:
                return None
            return _fingerprint(
                _analysis_identity_payload(
                    project_id=job.project_id,
                    phase=job.job_type.removesuffix("_analysis"),
                    request_key=request_key,
                    project_assets=project.source_assets,
                )
            )
        clips_manifest = self.repositories.clips.load(job.project_id)
        if job.job_type == "clip_export":
            clip = next(
                (item for item in clips_manifest.clips if item.clip_id == job.clip_id),
                None,
            )
            if clip is None:
                return None
            return _fingerprint(
                _export_identity_payload(
                    clip=clip,
                    project_assets=project.source_assets,
                    project_revision=project.revision,
                    clips_revision=clips_manifest.revision,
                )
            )
        clip = next(
            (item for item in clips_manifest.clips if item.clip_id == job.clip_id),
            None,
        )
        if clip is None:
            return None
        if job.job_type == "clip_render":
            media_binding = _project_media_binding(project)
            if media_binding is None or len(job.depends_on_job_ids) != 1:
                return None
            try:
                render_adapter = self.render_adapters.for_workflow(
                    str(clip.resolved_workflow)
                )
            except KeyError:
                return None
            if (
                render_adapter.name != job.adapter_name
                or render_adapter.version != job.adapter_version
            ):
                return None
            jobs_manifest = self.repositories.jobs.load(job.project_id)
            dependency_id = job.depends_on_job_ids[0]
            dependency = next(
                (
                    QueueJob.from_dict(item)
                    for item in jobs_manifest.jobs
                    if item.get("job_id") == dependency_id
                ),
                None,
            )
            if (
                dependency is None
                or self._current_trajectory_for_render(clip, (dependency,))
                != dependency
            ):
                return None
            workbench = _saved_workbench_reference(clip)
            if (
                workbench is None
                or not _workbench_binds_trajectory(
                    clip, dependency, workbench.value
                )
                or not _validate_workbench_immutable_output(
                    self.projects_root, job.project_id, clip, workbench
                )
            ):
                return None
            media_spec_revision, media_spec = media_binding
            try:
                physical_video, physical_map = _render_physical_inputs(
                    clip,
                    tuple(QueueJob.from_dict(item) for item in jobs_manifest.jobs),
                )
                return _fingerprint(
                    _render_identity_payload(
                        clip=clip,
                        project_revision=project.revision,
                        clips_revision=clips_manifest.revision,
                        trajectory=dependency,
                        workbench=workbench,
                        media_spec=media_spec,
                        media_spec_revision=media_spec_revision,
                        adapter_name=render_adapter.name,
                        adapter_version=render_adapter.version,
                        physical_video_path=physical_video,
                        physical_frame_map_path=physical_map,
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
        if job.job_type == "trajectory":
            try:
                adapter = self.adapters.for_workflow(str(clip.resolved_workflow))
            except KeyError:
                return None
            adapter_name = adapter.name
            adapter_version = adapter.version
        else:
            return None
        return _fingerprint(
            _job_identity_payload(
                job_type=job.job_type,
                clip=clip,
                project_assets=project.source_assets,
                project_revision=project.revision,
                clips_revision=clips_manifest.revision,
                adapter_name=adapter_name,
                adapter_version=adapter_version,
            )
        )

    def _prepare_clip_export(self, job: QueueJob) -> JobExecutionPlan:
        project = self.repositories.project.load(job.project_id)
        clips_manifest = self.repositories.clips.load(job.project_id)
        clip = next(
            (item for item in clips_manifest.clips if item.clip_id == job.clip_id),
            None,
        )
        if clip is None:
            raise KeyError(f"clip no longer exists: {job.clip_id}")
        video_path = _clip_asset_path(clip, project.source_assets, "video")
        if video_path is None or not video_path.is_file():
            raise FileNotFoundError("physical MP4 source is missing")
        attempt = Path(job.attempts[-1].directory)
        attempt.mkdir(parents=True, exist_ok=True)
        manifest_path = attempt / "export_manifest.json"
        output_dir = attempt / "clip_inputs"
        manifest_path.write_text(
            json.dumps(
                {
                    "clips": [
                        {
                            "clip_id": clip.clip_id,
                            **_authoritative_interval(clip),
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        command = (
            sys.executable,
            "-m",
            "cadscene.cli.export_video_clips",
            "--video",
            str(video_path),
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            "--progress-file",
            str(attempt / "adapter_progress.json"),
            "--allow-subset",
        )
        return JobExecutionPlan(
            commands=(command,),
            validate=lambda: _validate_clip_export_outputs((clip,), output_dir),
        )

    def _prepare_analysis(self, job: QueueJob) -> JobExecutionPlan:
        project = self.repositories.project.load(job.project_id)
        return prepare_analysis_plan(job, project.source_assets)

    def _attempt_directory(self, project_id: str, job_id: str, number: int) -> Path:
        return self.projects_root / project_id / "jobs" / job_id / f"attempt-{number}"

    @contextmanager
    def _state_guard(self, project_id: str) -> Iterator[None]:
        """Serialize project/clips/jobs snapshots before acquiring queue state."""
        with self._publication_lock:
            with ExitStack() as stack:
                for repository in (
                    self.repositories.project,
                    self.repositories.clips,
                    self.repositories.jobs,
                ):
                    stack.enter_context(repository.lock_for(project_id))
                stack.enter_context(self.queue.process_lock)
                yield

    def _publish_queue(self, project_id: str) -> JobsManifest:
        with self._state_guard(project_id):
            return self._publish_queue_locked(project_id)

    def _publish_queue_locked(self, project_id: str) -> JobsManifest:
        self.queue.require_publication(project_id)
        current = self.repositories.jobs.load(project_id)
        jobs = tuple(
            job.to_dict() for job in self.queue.jobs() if job.project_id == project_id
        )
        order = tuple(
            job_id
            for job_id in self.queue.queue_order()
            if self.queue.get(job_id).project_id == project_id
        )
        published = self.repositories.jobs.update(
            project_id,
            expected_revision=current.revision,
            mutate=lambda manifest: replace(
                manifest,
                jobs=jobs,
                queue_order=order,
            ),
        )
        self.queue.acknowledge_publication(project_id)
        return published


def _select_clips(
    clips: Sequence[ClipDefinition], clip_ids: Sequence[str] | None
) -> tuple[ClipDefinition, ...]:
    if clip_ids is None:
        return tuple(clips)
    selected = set(clip_ids)
    known = {clip.clip_id for clip in clips}
    missing = selected - known
    if missing:
        raise KeyError(f"unknown clip IDs: {sorted(missing)}")
    return tuple(clip for clip in clips if clip.clip_id in selected)


def _asset_path(assets: Mapping[str, object], name: str) -> Path | None:
    value = assets.get(f"{name}_path") or assets.get(name)
    if isinstance(value, Mapping):
        value = value.get("path")
    return None if value in (None, "") else Path(str(value))


def _clip_input_snapshot(clip: ClipDefinition) -> Mapping[str, object] | None:
    value = clip.analysis.get("input_snapshot")
    return value if isinstance(value, Mapping) else None


def _clip_asset_path(
    clip: ClipDefinition,
    project_assets: Mapping[str, object],
    name: str,
) -> Path | None:
    snapshot = _clip_input_snapshot(clip)
    if snapshot is not None:
        value = snapshot.get(name)
        if isinstance(value, Mapping):
            path = value.get("path")
            return None if path in (None, "") else Path(str(path))
        return None
    return _asset_path(project_assets, name)


def _clip_output_path(clip: ClipDefinition) -> Path | None:
    for key in ("physical_mp4_path", "export_path", "clip_path"):
        value = clip.analysis.get(key)
        if value:
            return Path(str(value))
    return None


def _clip_frame_map_path(clip: ClipDefinition) -> Path | None:
    value = clip.analysis.get("frame_map_path") or clip.analysis.get(
        "clip_frame_map_path"
    )
    return None if value in (None, "") else Path(str(value))


def _render_physical_inputs(
    clip: ClipDefinition, jobs: Sequence[QueueJob]
) -> tuple[Path, Path]:
    direct_video = _clip_output_path(clip)
    direct_map = _clip_frame_map_path(clip)
    if direct_video is not None and direct_map is not None:
        return direct_video, direct_map
    for job in reversed(tuple(jobs)):
        if (
            job.job_type != "clip_export"
            or job.clip_id != clip.clip_id
            or not _has_exact_success_proof(job)
        ):
            continue
        video = job.published_outputs.get(f"video:{clip.clip_id}")
        frame_map = job.published_outputs.get(f"frame_map:{clip.clip_id}")
        if isinstance(video, str) and video and isinstance(frame_map, str) and frame_map:
            return Path(video), Path(frame_map)
    raise ValueError("clip render physical inputs are unavailable")


def _load_authoritative_source_frames(
    clip: ClipDefinition, frame_map_path: Path
) -> tuple[DecodedFrameTimestamp, ...]:
    payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("authoritative frame map must be an object")
    raw_frames = payload.get("frames")
    if not isinstance(raw_frames, list):
        raw_clips = payload.get("clips")
        if not isinstance(raw_clips, list):
            raise ValueError("authoritative frame map has no clip frames")
        selected = next(
            (
                item
                for item in raw_clips
                if isinstance(item, Mapping) and item.get("clip_id") == clip.clip_id
            ),
            None,
        )
        raw_frames = None if selected is None else selected.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("authoritative frame map has no clip frames")
    frames: list[DecodedFrameTimestamp] = []
    for item in raw_frames:
        if not isinstance(item, Mapping):
            raise ValueError("authoritative frame map entry must be an object")
        ordinal = item.get("ordinal", item.get("source_decoded_frame_ordinal"))
        pts = item.get("pts", item.get("source_pts"))
        duration = item.get("duration_pts")
        if (
            isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or isinstance(pts, bool)
            or not isinstance(pts, int)
            or (
                duration is not None
                and (isinstance(duration, bool) or not isinstance(duration, int))
            )
        ):
            raise ValueError("authoritative frame identity must use integers")
        frames.append(DecodedFrameTimestamp(ordinal, pts, duration, "pts"))
    for previous, current in zip(frames, frames[1:]):
        if current.ordinal != previous.ordinal + 1 or current.pts <= previous.pts:
            raise ValueError("authoritative source frames are not contiguous")
    start = int(clip.analysis["source_start_pts"])
    end = int(clip.analysis["source_end_pts_exclusive"])
    if frames[0].pts < start or frames[-1].pts >= end:
        raise ValueError("authoritative source frames escape the clip interval")
    return tuple(frames)


def _workbench_artifact_path(
    projects_root: Path, project_id: str, workbench: StateReference
) -> Path:
    revision = str(workbench.value["workbench_output_revision"])
    revision_root = (
        projects_root / project_id / "workbench_outputs" / revision
    ).resolve(strict=True)
    manifest = json.loads(
        (revision_root / "workbench_output_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    relative = manifest["artifacts"]["camera_track"]["path"]
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("workbench artifact path is invalid")
    artifact = (revision_root / relative).resolve(strict=True)
    artifact.relative_to(revision_root)
    if not artifact.is_file():
        raise ValueError("workbench artifact is missing")
    return artifact


def _validate_existing_render_publication(
    target: Path,
    *,
    job: QueueJob,
    output_revision: str,
    output_fingerprint: str,
    proof: Mapping[str, object],
) -> Mapping[str, str]:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("immutable render revision target is not a directory")
    resolved_target = target.resolve(strict=True)
    if resolved_target != target.absolute():
        raise ValueError("immutable render revision target changes path identity")
    paths = {
        "video": target / "rendered.mp4",
        "frame_map": target / "render_frame_map.json",
        "manifest": target / "render_output_manifest.json",
    }
    try:
        for path in paths.values():
            if path.is_symlink() or not path.is_file():
                raise ValueError("immutable render revision file is missing or unsafe")
            path.resolve(strict=True).relative_to(resolved_target)
        payload = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("immutable render revision manifest is invalid")
        expected_identity = {
            "schema_version": 1,
            "project_id": job.project_id,
            "clip_id": job.clip_id,
            "job_id": job.job_id,
            "input_revision": job.input_revision,
            "input_fingerprint": job.input_fingerprint,
            "output_revision": output_revision,
            "output_fingerprint": output_fingerprint,
            "adapter_name": job.adapter_name,
            "adapter_version": job.adapter_version,
            "validation_proof": proof,
        }
        if any(payload.get(key) != value for key, value in expected_identity.items()):
            raise ValueError("immutable render revision identity collision")
        if (
            sha256(paths["video"].read_bytes()).hexdigest()
            != proof.get("video_sha256")
            or sha256(paths["frame_map"].read_bytes()).hexdigest()
            != proof.get("frame_map_sha256")
        ):
            raise ValueError("immutable render revision content hash mismatch")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("immutable render revision cannot be validated") from exc
    return {name: str(path) for name, path in paths.items()}


def _fsync_regular_file(path: Path) -> None:
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())


def _fsync_parent_directory(path: Path) -> bool:
    """Persist a directory entry where the host supports directory fsync."""
    if os.name == "nt":
        return False
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _fraction_time_base(clip: ClipDefinition) -> Fraction:
    value = clip.analysis.get("source_time_base")
    if not isinstance(value, Mapping):
        raise ValueError("clip is missing source_time_base")
    return Fraction(int(value["numerator"]), int(value["denominator"]))


def _authoritative_interval(clip: ClipDefinition) -> Mapping[str, object]:
    required = (
        "source_start_pts",
        "source_end_pts_exclusive",
        "source_time_base",
        "interval_semantics",
    )
    interval = {key: clip.analysis.get(key) for key in required}
    if interval["interval_semantics"] != "half_open":
        raise ValueError("clip interval must use authoritative half-open semantics")
    if any(interval[key] is None for key in required):
        raise ValueError("clip is missing authoritative integer-PTS interval fields")
    return interval


def _asset_fingerprints(assets: Mapping[str, object]) -> Mapping[str, object]:
    fingerprints: dict[str, object] = {}
    for name in ("video", "srt"):
        path = _asset_path(assets, name)
        if path is None:
            fingerprints[name] = None
        elif path.is_file():
            stat = path.stat()
            fingerprints[name] = {
                "path": str(path.resolve()),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        else:
            fingerprints[name] = {"path": str(path), "missing": True}
    return fingerprints


def _clip_input_identity(
    clip: ClipDefinition, project_assets: Mapping[str, object]
) -> Mapping[str, object]:
    snapshot = _clip_input_snapshot(clip)
    if snapshot is None:
        return _asset_fingerprints(project_assets)
    identity: dict[str, object] = {}
    for name in ("video", "cad", "srt", "analysis_artifact"):
        value = snapshot.get(name)
        if not isinstance(value, Mapping):
            identity[name] = None
            continue
        identity[name] = {
            key: value.get(key)
            for key in (
                "path",
                "sha256",
                "dataset_id",
                "dataset_path",
                "artifact_id",
            )
            if value.get(key) is not None
        }
    identity["request_key"] = snapshot.get("request_key")
    return identity


def _project_media_binding(
    project: ProjectManifest,
) -> tuple[str, ProjectMediaSpec] | None:
    if project.media_spec_revision is None or project.media_spec is None:
        return None
    try:
        return project.media_spec_revision, ProjectMediaSpec.from_dict(project.media_spec)
    except (TypeError, ValueError):
        return None


def _media_spec_revision(spec: ProjectMediaSpec) -> str:
    fingerprint = sha256(
        json.dumps(
            spec.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return f"media-spec-{fingerprint[:16]}"


def _workbench_render_parameters(
    storage_root: Path, project_id: str, clip: ClipDefinition
) -> dict[str, object]:
    snapshot = clip.analysis.get("input_snapshot")
    cad = snapshot.get("cad") if isinstance(snapshot, Mapping) else None
    cad_path = cad.get("dataset_path") if isinstance(cad, Mapping) else None
    parameters: dict[str, object] = {}
    if isinstance(cad_path, str) and cad_path:
        parameters["cad_dataset_path"] = cad_path
    manifest_path = (
        Path(storage_root)
        / "data"
        / f"{project_id}-{clip.clip_id}"
        / "dataset_manifest.json"
    )
    defaults: Mapping[str, object] = {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if isinstance(payload, Mapping) and isinstance(payload.get("defaults"), Mapping):
            defaults = payload["defaults"]
    except (OSError, json.JSONDecodeError):
        pass
    parameters["cad_scale"] = defaults.get("cad_scale", 0.06)
    parameters["origin_xy"] = defaults.get("origin_xy", [0.0, 0.0])
    return parameters


def _trajectory_artifact_matches_proof(job: QueueJob) -> bool:
    trajectory_path = job.published_outputs.get("trajectory")
    if (
        not isinstance(trajectory_path, str)
        or not job.output_fingerprint
        or not job.attempts
    ):
        return False
    try:
        attempt_root = Path(job.attempts[-1].directory).resolve(strict=True)
        path = Path(trajectory_path).resolve(strict=True)
        path.relative_to(attempt_root)
        return (
            path.is_file()
            and sha256(path.read_bytes()).hexdigest() == job.output_fingerprint
        )
    except (OSError, ValueError):
        return False


def _saved_workbench_reference(clip: ClipDefinition) -> StateReference | None:
    for reference in reversed(clip.references):
        if (
            reference.owner == "clips"
            and reference.key == f"workbench:{clip.clip_id}"
            and reference.value.get("status") == "saved"
        ):
            return reference
    return None


def _workbench_binds_trajectory(
    clip: ClipDefinition,
    trajectory: QueueJob,
    workbench: Mapping[str, object],
) -> bool:
    revision = workbench.get("workbench_output_revision")
    fingerprint = workbench.get("workbench_output_fingerprint")
    return (
        workbench.get("workflow") == clip.resolved_workflow
        and workbench.get("input_revision") == clip.analysis_revision
        and workbench.get("input_fingerprint") == trajectory.input_fingerprint
        and workbench.get("trajectory_job_id") == trajectory.job_id
        and workbench.get("trajectory_output_revision")
        == trajectory.output_revision
        and workbench.get("trajectory_output_fingerprint")
        == trajectory.output_fingerprint
        and isinstance(revision, str)
        and bool(revision)
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
    )


def _validate_workbench_immutable_output(
    projects_root: Path,
    project_id: str,
    clip: ClipDefinition,
    workbench: StateReference,
) -> bool:
    revision = workbench.value.get("workbench_output_revision")
    expected_fingerprint = workbench.value.get("workbench_output_fingerprint")
    if not isinstance(revision, str) or not isinstance(expected_fingerprint, str):
        return False
    try:
        output_root = (projects_root / project_id / "workbench_outputs").resolve(
            strict=True
        )
        revision_root = (output_root / revision).resolve(strict=True)
        revision_root.relative_to(output_root)
        manifest_path = (revision_root / "workbench_output_manifest.json").resolve(
            strict=True
        )
        manifest_path.relative_to(revision_root)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            return False
        expected = {
            "project_id": project_id,
            "clip_id": clip.clip_id,
            "workflow": clip.resolved_workflow,
            "workbench_output_revision": revision,
            "workbench_output_fingerprint": expected_fingerprint,
            "operation_id": workbench.operation_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            return False
        unhashed = dict(payload)
        unhashed.pop("workbench_output_fingerprint", None)
        serialized = (
            json.dumps(unhashed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if sha256(serialized).hexdigest() != expected_fingerprint:
            return False
        artifacts = payload.get("artifacts")
        camera_track = (
            artifacts.get("camera_track") if isinstance(artifacts, Mapping) else None
        )
        if not isinstance(camera_track, Mapping):
            return False
        relative_path = camera_track.get("path")
        artifact_hash = camera_track.get("sha256")
        size_bytes = camera_track.get("size_bytes")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or not isinstance(artifact_hash, str)
            or len(artifact_hash) != 64
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or size_bytes < 0
        ):
            return False
        artifact_path = (revision_root / relative_path).resolve(strict=True)
        artifact_path.relative_to(revision_root)
        if not artifact_path.is_file():
            return False
        artifact_bytes = artifact_path.read_bytes()
        return (
            len(artifact_bytes) == size_bytes
            and sha256(artifact_bytes).hexdigest() == artifact_hash
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _render_identity_payload(
    *,
    clip: ClipDefinition,
    project_revision: int,
    clips_revision: int,
    trajectory: QueueJob,
    workbench: StateReference,
    media_spec: ProjectMediaSpec,
    media_spec_revision: str,
    adapter_name: str,
    adapter_version: str,
    physical_video_path: Path | None = None,
    physical_frame_map_path: Path | None = None,
) -> Mapping[str, object]:
    return {
        "job_type": "clip_render",
        "clip_id": clip.clip_id,
        "project_manifest_revision": project_revision,
        "clips_manifest_revision": clips_revision,
        "clip_interval": _authoritative_interval(clip),
        "analysis_revision": clip.analysis_revision,
        "resolved_workflow": clip.resolved_workflow,
        "parameters": dict(clip.manual_definition),
        "physical_inputs": _render_input_asset_identity(
            clip,
            video_path=physical_video_path,
            frame_map_path=physical_frame_map_path,
        ),
        "trajectory": {
            "job_id": trajectory.job_id,
            "input_revision": trajectory.input_revision,
            "input_fingerprint": trajectory.input_fingerprint,
            "output_revision": trajectory.output_revision,
            "output_fingerprint": trajectory.output_fingerprint,
            "validated_input_fingerprint": trajectory.validated_input_fingerprint,
        },
        "workbench": {
            "operation_id": workbench.operation_id,
            "output_revision": workbench.value.get("workbench_output_revision"),
            "output_fingerprint": workbench.value.get(
                "workbench_output_fingerprint"
            ),
        },
        "project_media_spec_revision": media_spec_revision,
        "project_media_spec": media_spec.to_dict(),
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
    }


def _render_input_asset_identity(
    clip: ClipDefinition,
    *,
    video_path: Path | None = None,
    frame_map_path: Path | None = None,
) -> Mapping[str, object]:
    video_path = video_path or _clip_output_path(clip)
    frame_map_path = frame_map_path or _clip_frame_map_path(clip)
    if video_path is None or frame_map_path is None:
        raise ValueError("clip render physical inputs are unavailable")
    clip_time_base = clip.analysis.get("source_time_base")
    if not isinstance(clip_time_base, Mapping):
        raise ValueError("clip is missing source time base")
    expected_numerator = clip_time_base.get("numerator")
    expected_denominator = clip_time_base.get("denominator")
    if (
        type(expected_numerator) is not int
        or type(expected_denominator) is not int
        or expected_numerator <= 0
        or expected_denominator <= 0
    ):
        raise ValueError("clip source time base is invalid")
    try:
        if video_path.is_symlink() or frame_map_path.is_symlink():
            raise ValueError("clip render physical input is unsafe")
        video = video_path.resolve(strict=True)
        frame_map = frame_map_path.resolve(strict=True)
        if not video.is_file() or not frame_map.is_file():
            raise ValueError("clip render physical input is not a regular file")
        payload = json.loads(frame_map.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("authoritative frame map must be an object")
        raw_time_base = payload.get("source_time_base")
        if not isinstance(raw_time_base, Mapping):
            raise ValueError("authoritative frame map has no source time base")
        numerator = raw_time_base.get("numerator")
        denominator = raw_time_base.get("denominator")
        if (
            type(numerator) is not int
            or type(denominator) is not int
            or numerator != expected_numerator
            or denominator != expected_denominator
        ):
            raise ValueError("authoritative frame map time base differs from clip")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("clip render physical input cannot be validated") from exc

    def identity(path: Path) -> Mapping[str, object]:
        content = path.read_bytes()
        return {
            "path": str(path),
            "size_bytes": len(content),
            "sha256": sha256(content).hexdigest(),
        }

    return {"video": identity(video), "frame_map": identity(frame_map)}


def _fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _job_identity_payload(
    *,
    job_type: str,
    clip: ClipDefinition,
    project_assets: Mapping[str, object],
    project_revision: int,
    clips_revision: int,
    adapter_name: str,
    adapter_version: str,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "job_type": job_type,
        "clip_id": clip.clip_id,
        "clip_interval": _authoritative_interval(clip),
        "analysis_revision": clip.analysis_revision,
        "resolved_workflow": clip.resolved_workflow,
        "source_assets": _clip_input_identity(clip, project_assets),
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "parameters": dict(clip.manual_definition),
    }
    if _clip_input_snapshot(clip) is None:
        payload.update(
            {
                "project_manifest_revision": project_revision,
                "clips_manifest_revision": clips_revision,
            }
        )
    return payload


def _analysis_identity_assets(
    project_assets: Mapping[str, object],
) -> Mapping[str, object]:
    assets: dict[str, object] = {}
    for name in ("video", "cad", "srt"):
        value = project_assets.get(name)
        if not isinstance(value, Mapping):
            assets[name] = None
            continue
        assets[name] = {
            "path": None if value.get("path") is None else str(value.get("path")),
            "sha256": None if value.get("sha256") is None else str(value.get("sha256")),
        }
    return assets


def _analysis_identity_payload(
    *,
    project_id: str,
    phase: str,
    request_key: str,
    project_assets: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "identity_schema": ANALYSIS_IDENTITY_SCHEMA,
        "project_id": project_id,
        "job_type": f"{phase}_analysis",
        "request_key": request_key,
        "source_assets": _analysis_identity_assets(project_assets),
        "adapter_name": ANALYSIS_ADAPTER_NAME,
        "adapter_version": ANALYSIS_ADAPTER_VERSION,
    }


def _legacy_analysis_identity_payload(
    *,
    phase: str,
    request_key: str,
    project_assets: Mapping[str, object],
) -> Mapping[str, object]:
    """Return the pre-project-scope identity only for one-time restore."""

    return {
        "job_type": f"{phase}_analysis",
        "request_key": request_key,
        "source_assets": _analysis_identity_assets(project_assets),
        "adapter_name": ANALYSIS_ADAPTER_NAME,
        "adapter_version": ANALYSIS_ADAPTER_VERSION,
    }


def _analysis_job_contract(
    *,
    project_id: str,
    phase: str,
    request_key: str,
    project_assets: Mapping[str, object],
) -> dict[str, object]:
    if phase not in {"cad", "video"}:
        raise ValueError(f"unsupported analysis phase: {phase}")
    identity_payload = _analysis_identity_payload(
        project_id=project_id,
        phase=phase,
        request_key=request_key,
        project_assets=project_assets,
    )
    return {
        "project_id": project_id,
        "clip_id": "__project__",
        "job_type": f"{phase}_analysis",
        "resource_class": "light_compute",
        "priority": 10,
        "exclusive_key": f"analysis:{project_id}:{phase}",
        "idempotency_key": _fingerprint(
            {**identity_payload, "purpose": "idempotency"}
        ),
        "input_revision": request_key,
        "input_fingerprint": _fingerprint(identity_payload),
        "adapter_name": ANALYSIS_ADAPTER_NAME,
        "adapter_version": ANALYSIS_ADAPTER_VERSION,
    }


def _analysis_request_key_from_assets(
    assets: Mapping[str, object]
) -> str | None:
    fingerprints: dict[str, str] = {}
    for required in ("video", "cad"):
        value = assets.get(required)
        if not isinstance(value, Mapping) or not value.get("path") or not value.get("sha256"):
            return None
        fingerprints[required] = str(value["sha256"])
    srt = assets.get("srt")
    if isinstance(srt, Mapping) and srt.get("sha256"):
        fingerprints["srt"] = str(srt["sha256"])
    return _fingerprint(fingerprints)


def _analysis_input_snapshot(
    project_assets: Mapping[str, object],
    *,
    request_key: str,
    cad_dataset_id: str,
    cad_dataset_path: Path,
    analysis_artifact_id: str,
    analysis_artifact_path: Path,
) -> Mapping[str, object]:
    snapshot: dict[str, object] = {"request_key": request_key}
    for name in ("video", "cad", "srt"):
        value = project_assets.get(name)
        if not isinstance(value, Mapping):
            snapshot[name] = None
            continue
        snapshot[name] = {
            key: value.get(key)
            for key in (
                "path",
                "sha256",
                "size_bytes",
                "original_filename",
                "validation_report",
            )
            if value.get(key) is not None
        }
    cad = dict(snapshot.get("cad") or {})
    cad.update(
        {"dataset_id": cad_dataset_id, "dataset_path": str(cad_dataset_path)}
    )
    snapshot["cad"] = cad
    snapshot["analysis_artifact"] = {
        "artifact_id": analysis_artifact_id,
        "path": str(analysis_artifact_path),
    }
    return snapshot


def _export_identity_payload(
    *,
    clip: ClipDefinition,
    project_assets: Mapping[str, object],
    project_revision: int,
    clips_revision: int,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "job_type": "clip_export",
        "clip_id": clip.clip_id,
        "analysis_revision": clip.analysis_revision,
        "clip_interval": _authoritative_interval(clip),
        "source_assets": _clip_input_identity(clip, project_assets),
        "adapter_name": "clip_export",
        "adapter_version": "1",
        "parameters": dict(clip.manual_definition),
    }
    if _clip_input_snapshot(clip) is None:
        payload.update(
            {
                "project_manifest_revision": project_revision,
                "clips_manifest_revision": clips_revision,
            }
        )
    return payload


def _validate_clip_export_outputs(
    clips: Sequence[ClipDefinition], output_dir: Path
) -> AdapterResult:
    frame_map_path = output_dir / "clip_frame_map.json"
    if not frame_map_path.is_file():
        return AdapterResult.failed("clip export frame map is missing")
    try:
        payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return AdapterResult.failed(f"invalid clip export frame map: {exc}")
    mapped = {str(item.get("clip_id")): item for item in payload.get("clips", ())}
    outputs: dict[str, str] = {}
    digest = sha256(frame_map_path.read_bytes())
    for clip in clips:
        item = mapped.get(clip.clip_id)
        interval = _authoritative_interval(clip)
        if item is None or any(
            item.get(key) != interval[key]
            for key in ("source_start_pts", "source_end_pts_exclusive")
        ):
            return AdapterResult.failed(
                f"clip export frame map mismatch for {clip.clip_id}"
            )
        video_path = output_dir / f"{clip.clip_id}.mp4"
        if not video_path.is_file() or video_path.stat().st_size == 0:
            return AdapterResult.failed(f"physical clip is missing: {clip.clip_id}")
        digest.update(video_path.read_bytes())
        outputs[f"video:{clip.clip_id}"] = str(video_path)
        outputs[f"frame_map:{clip.clip_id}"] = str(frame_map_path)
    fingerprint = digest.hexdigest()
    return AdapterResult.success(
        output_revision=f"clip_export:{fingerprint[:16]}",
        output_fingerprint=fingerprint,
        outputs=outputs,
    )
