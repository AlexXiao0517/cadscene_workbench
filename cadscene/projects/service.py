from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import sys
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
    register_analysis_revision,
)
from .repositories import ManifestMutation, RevisionConflict, publish_manifests
from .uploads import PublishedUpload
from .queue import (
    AttemptRecord,
    LocalResourceQueue,
    PreparedSubmissionBatch,
    QueueJob,
    RestoreCleanupReservation,
)


ANALYSIS_IDENTITY_SCHEMA = 2


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
class EnqueueAnalysisResult:
    job_ids: tuple[str, str]
    request_key: str


@dataclass(frozen=True)
class RegisterUploadResult:
    project_revision: int
    request_key: str | None
    analysis_job_ids: tuple[str, ...] = ()


class _AnalysisPublicationPending(RuntimeError):
    def __init__(self, project_id: str, job_id: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.project_id = project_id
        self.job_id = job_id
        self.cause = cause


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
    ) -> None:
        self.repositories = repositories
        self.queue = queue
        self.adapters = adapters
        self.projects_root = Path(projects_root)
        self.storage_root = self.projects_root.parent
        self.now = now
        self._identity = identity or (lambda: uuid4().hex)
        self.analysis_publisher = AnalysisArtifactPublisher(
            storage_root=self.storage_root,
            projects_root=self.projects_root,
            identity=self._identity,
        )
        self._publication_lock = threading.RLock()
        self.queue.enable_publication_gate()

    def register_uploaded_asset(
        self,
        project_id: str,
        upload: PublishedUpload,
        *,
        expected_revision: int,
    ) -> RegisterUploadResult:
        """Atomically register immutable upload state and any new analysis DAG."""

        if upload.project_id != project_id:
            raise ValueError("published upload belongs to another project")
        if not upload.path.is_file() or not upload.validation_report_path.is_file():
            raise FileNotFoundError("immutable upload media/report is unavailable")
        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            if project.revision != expected_revision:
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
            request_key = _analysis_request_key_from_assets(assets)
            previous = assets.get("_analysis")
            previous_key = (
                str(previous.get("request_key"))
                if isinstance(previous, Mapping) and previous.get("request_key")
                else None
            )
            batch: PreparedSubmissionBatch | None = None
            prepared: tuple[QueueJob, ...] = ()
            job_ids: tuple[str, ...] = ()
            current_jobs = self.repositories.jobs.load(project_id)
            if request_key is not None and request_key != previous_key:
                batch = self._prepare_analysis_submission(
                    project_id,
                    request_key=request_key,
                    project_assets=assets,
                )
                prepared = batch.jobs
                job_ids = batch.job_ids

            existing_jobs = tuple(
                item for item in self.queue.jobs() if item.project_id == project_id
            )
            order = tuple(
                dict.fromkeys(
                    (
                        *(
                            job_id
                            for job_id in self.queue.queue_order()
                            if self.queue.get(job_id).project_id == project_id
                        ),
                        *(item.job_id for item in prepared),
                    )
                )
            )
            by_id = {item.job_id: item for item in existing_jobs}
            by_id.update({item.job_id: item for item in prepared})

            def mutate_project(
                value: ProjectManifest, operation_id: str
            ) -> ProjectManifest:
                published_assets = dict(assets)
                project_state = value.project_state
                if prepared:
                    assert batch is not None and request_key is not None
                    base_state = dict(
                        published_assets.get("_analysis", {})
                        if isinstance(
                            published_assets.get("_analysis"), Mapping
                        )
                        else {}
                    )
                    base_state.setdefault("requested_at", self.now())
                    state, project_state = self._analysis_state_for_submission(
                        batch=batch,
                        project=value,
                        project_assets=published_assets,
                        request_key=request_key,
                        base_state=base_state,
                        operation_id=operation_id,
                    )
                    published_assets["_analysis"] = state
                return replace(
                    value,
                    updated_at=self.now(),
                    source_assets=published_assets,
                    project_state=project_state,
                )

            mutations = [
                ManifestMutation(
                    repository=self.repositories.project,
                    project_id=project_id,
                    expected_revision=project.revision,
                    mutate=mutate_project,
                )
            ]
            if prepared:
                def mutate_jobs(
                    value: JobsManifest, operation_id: str
                ) -> JobsManifest:
                    assert batch is not None
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

                mutations.append(
                    ManifestMutation(
                        repository=self.repositories.jobs,
                        project_id=project_id,
                        expected_revision=current_jobs.revision,
                        mutate=mutate_jobs,
                    )
                )
            try:
                publication = publish_manifests(mutations)
            except Exception as publication_error:
                from .recovery import reconcile_project

                reconcile_project(project_id, repositories=self.repositories)
                recovered_project = self.repositories.project.load(project_id)
                recovered_asset = recovered_project.source_assets.get(
                    upload.asset_type
                )
                recovered_jobs = self.repositories.jobs.load(project_id)
                recovered_by_id = {
                    str(item["job_id"]): QueueJob.from_dict(item)
                    for item in recovered_jobs.jobs
                }
                asset_recovered = (
                    isinstance(recovered_asset, Mapping)
                    and recovered_asset.get("path") == str(upload.path)
                    and recovered_asset.get("sha256") == upload.sha256
                )
                jobs_recovered = all(
                    job_id in recovered_by_id for job_id in job_ids
                )
                if not asset_recovered or (prepared and not jobs_recovered):
                    raise publication_error
                if prepared:
                    assert batch is not None
                    self._commit_analysis_submission(batch, recovered_by_id)
                    self.queue.acknowledge_publication(project_id)
                return RegisterUploadResult(
                    project_revision=recovered_project.revision,
                    request_key=request_key,
                    analysis_job_ids=job_ids,
                )
            if prepared:
                jobs = next(
                    item
                    for item in publication.manifests
                    if isinstance(item, JobsManifest)
                )
                persisted = {
                    str(item["job_id"]): QueueJob.from_dict(item)
                    for item in jobs.jobs
                }
                assert batch is not None
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
            restored = self.queue.merge_restored(
                manifest.jobs,
                project_id=project_id,
                queue_order=manifest.queue_order,
                process_probe=process_probe,
                current_fingerprint_resolver=self._current_input_fingerprint,
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
            if (
                job.validated_input_fingerprint is not None
                and job.validated_input_fingerprint != legacy_fingerprint
            ):
                raise ValueError(
                    "legacy validated input fingerprint does not match"
                )
            if job.output_validated and job.validated_input_fingerprint is None:
                raise ValueError(
                    "validated legacy analysis output has no input fingerprint"
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
        if not analysis_jobs:
            return
        successful_video = next(
            (
                item
                for item in analysis_jobs
                if item.job_type == "video_analysis" and item.status == "success"
            ),
            None,
        )
        terminal_problem = next(
            (
                item
                for item in analysis_jobs
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
        if successful_video is not None:
            status = "success"
            error = None
        elif terminal_problem is not None:
            status = terminal_problem.status
            error = terminal_problem.error
        elif any(item.status in {"preparing", "running", "validating"} for item in analysis_jobs):
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
            all(
                item.status == "success" and item.output_validated
                for item in jobs
            )
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
    value = clip.analysis.get("frame_map_path")
    return None if value in (None, "") else Path(str(value))


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
