from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from fractions import Fraction
from hashlib import sha256
import json
from math import isfinite
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
from .concat import (
    ConcatClip,
    ConcatPreflight,
    ConcatPreflightRequest,
    RenderCandidate,
    build_concat_plan,
    preflight_concat,
)
from .concat_adapters import ConcatMediaAdapter, ConcatMediaInputs
from .concat_executor import validate_concat_outputs
from .catalog import (
    ProjectSummary,
    build_project_summary,
    sort_project_summaries,
    unavailable_project_summary,
)
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
    VideoMediaInfo,
    media_compatibility,
    probe_media,
    probe_project_media_spec,
    validate_rendered_media,
)
from .identifiers import is_safe_stable_id
from .render_adapters import RenderAdapterRegistry
from .render_adapters import RenderInputs
from .source_fallback import SourceIntervalRenderAdapter, SourceIntervalRenderInputs
from .scene_bridge_runner import (
    BRIDGE_ALGORITHM_VERSION,
    SceneBridgeInputs,
    validate_scene_bridge_candidate,
)
from .scene_bridges import SolveInterval, derive_scene_solve_interval
from .repositories import ManifestMutation, RevisionConflict, publish_manifests
from .uploads import PublishedUpload
from .queue import (
    AttemptRecord,
    LocalResourceQueue,
    PreparedSubmissionBatch,
    QueueJob,
    RestoreCleanupReservation,
)
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp
from cadscene.workflow.job_runner import read_workflow_log_text
from cadscene.application_resources import application_root
from cadscene.srt.georeference import (
    CadGeoreference,
    CrsCandidate,
    parse_central_meridian,
    recommend_cgcs2000_candidates,
)
from cadscene.srt.parser import load_srt_records


ANALYSIS_IDENTITY_SCHEMA = 2
CAD_GEOREFERENCE_CANDIDATE_ADAPTER_NAME = "cad_georeference_candidates"
CAD_GEOREFERENCE_CANDIDATE_ADAPTER_VERSION = "2"


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
class EnqueueSceneBridgeResult:
    job: QueueJob
    source_clip_id: str
    target_clip_id: str
    direction: str


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
class EnqueueMergeResult:
    job_id: str
    preflight: ConcatPreflight


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


@dataclass(frozen=True)
class EnqueueCadReplacementResult:
    job_id: str
    project_revision: int
    candidate_revision: str


class ProjectDeletionBlocked(RuntimeError):
    """Raised when deleting a project would race active project work."""

    code = "project_deletion_blocked"

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


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


class _SceneBridgePublicationPending(RuntimeError):
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
        concat_adapter: ConcatMediaAdapter | None = None,
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
        self.concat_adapter = concat_adapter or ConcatMediaAdapter(
            validator=self._validate_concat_execution
        )
        self.analysis_publisher = AnalysisArtifactPublisher(
            storage_root=self.storage_root,
            projects_root=self.projects_root,
            identity=self._identity,
        )
        self._publication_lock = threading.RLock()
        from cadscene.annotations.service import AnnotationService

        self.annotation_service = AnnotationService(
            repositories,
            now=now,
            identity=self._identity,
            publication_lock=self._publication_lock,
        )
        from cadscene.annotations.tracking import (
            OpenCvLkVideoAnchorTracker,
            TrackingRevisionRepository,
            VideoTrackingService,
        )

        self.tracking_revision_repository = TrackingRevisionRepository(
            self.projects_root
        )
        self.video_tracking_service = VideoTrackingService(
            repositories=repositories,
            annotations=self.annotation_service,
            revisions=self.tracking_revision_repository,
            tracker=OpenCvLkVideoAnchorTracker(),
            now=now,
            identity=self._identity,
        )
        self.queue.enable_publication_gate()

    def list_projects(self) -> tuple[ProjectSummary, ...]:
        """枚举本地项目；单个损坏项目不会阻断整个项目库。"""

        summaries: list[ProjectSummary] = []
        if not self.projects_root.is_dir():
            return ()
        for directory in self.projects_root.iterdir():
            if not directory.is_dir() or not is_safe_stable_id(directory.name):
                continue
            project_id = directory.name
            try:
                summaries.append(
                    build_project_summary(
                        self.repositories.project.load(project_id),
                        self.repositories.clips.load(project_id),
                        self.repositories.jobs.load(project_id),
                        self.repositories.render.load(project_id),
                        self.repositories.annotations.load(project_id),
                    )
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                summaries.append(unavailable_project_summary(project_id))
        return sort_project_summaries(summaries)

    def delete_project_workspace(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> None:
        """永久删除项目自有工作空间，不跟随 manifest 中的外部源文件路径。"""

        with self._publication_lock:
            with ExitStack() as stack:
                for repository in self.repositories.in_lock_order():
                    stack.enter_context(repository.lock_for(project_id))
                stack.enter_context(self.queue.process_lock)
                project = self.repositories.project.load(project_id)
                if project.revision != expected_revision:
                    raise RevisionConflict(
                        project_id=project_id,
                        expected_revision=expected_revision,
                        current_revision=project.revision,
                    )
                active_jobs = tuple(
                    job.job_id
                    for job in self.queue.jobs()
                    if job.project_id == project_id
                    and job.status
                    in {
                        "queued",
                        "preparing",
                        "running",
                        "validating",
                        "publishing",
                        "cancelling",
                    }
                )
                if active_jobs:
                    raise ProjectDeletionBlocked(
                        "active_jobs",
                        "project has active jobs; cancel them before deletion",
                    )
                if _has_active_workbench_session(
                    self.projects_root / project_id / "workbench_sessions",
                    project_id=project_id,
                    now=self.now(),
                ):
                    raise ProjectDeletionBlocked(
                        "active_workbench_session",
                        "project has an active workbench session; close it before deletion",
                    )
                clips = self.repositories.clips.load(project_id)
                self._delete_project_owned_paths(
                    project_id,
                    tuple(clip.clip_id for clip in clips.clips),
                )

    def _delete_project_owned_paths(
        self, project_id: str, clip_ids: tuple[str, ...]
    ) -> None:
        storage_root = self.storage_root.resolve(strict=True)
        projects_root = self.projects_root.resolve(strict=True)
        project_path = self.projects_root / project_id
        targets: list[tuple[str, Path]] = []
        for clip_id in clip_ids:
            if not is_safe_stable_id(clip_id):
                raise ValueError(f"unsafe clip_id in project manifest: {clip_id}")
            for owner in ("data", "runs"):
                path = self.storage_root / owner / f"{project_id}-{clip_id}"
                if path.exists():
                    _require_owned_deletion_target(path, storage_root)
                    targets.append((f"{owner}-{clip_id}", path))
        _require_owned_deletion_target(project_path, projects_root)
        targets.append(("project", project_path))

        deletion_root = self.storage_root / ".project-deletions"
        _require_owned_deletion_target(deletion_root, storage_root)
        operation = sha256(
            f"{project_id}:{self._identity()}".encode("utf-8")
        ).hexdigest()[:20]
        staging = deletion_root / operation
        staging.mkdir(parents=True, exist_ok=False)
        moved: list[tuple[Path, Path]] = []
        try:
            for name, source in targets:
                destination = staging / name
                os.replace(source, destination)
                moved.append((source, destination))
        except Exception:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    os.replace(destination, source)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(staging)
        try:
            deletion_root.rmdir()
        except OSError:
            pass

    def track_video_annotation(
        self,
        project_id: str,
        annotation_id: str,
        *,
        expected_revision: int,
        expected_annotation_revision: int,
        correction=None,
    ):
        from cadscene.annotations.tracking import decode_tracking_frames

        project = self.repositories.project.load(project_id)
        annotations = self.repositories.annotations.load(project_id)
        annotation = next(
            (item for item in annotations.annotations if item.annotation_id == annotation_id),
            None,
        )
        if annotation is None:
            raise FileNotFoundError(f"annotation not found: {annotation_id}")
        clips = self.repositories.clips.load(project_id)
        clip = next(
            (item for item in clips.clips if item.clip_id == annotation.clip_id), None
        )
        if clip is None:
            raise ValueError(f"annotation clip is unavailable: {annotation.clip_id}")
        descriptor = project.source_assets.get("video")
        if isinstance(descriptor, Mapping):
            path_value = descriptor.get("path")
            fingerprint_value = descriptor.get("sha256")
        else:
            path_value = project.source_assets.get("video_path")
            fingerprint_value = None
        source = Path(str(path_value or ""))
        if not source.is_file():
            raise FileNotFoundError("project source video is unavailable for tracking")
        if isinstance(fingerprint_value, str) and fingerprint_value:
            source_fingerprint = fingerprint_value
        else:
            digest = sha256()
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            source_fingerprint = digest.hexdigest()
        time_base_payload = clip.analysis.get("source_time_base")
        if not isinstance(time_base_payload, Mapping):
            raise ValueError("tracking clip is missing exact source time_base")
        time_base = Fraction(
            int(time_base_payload["numerator"]),
            int(time_base_payload["denominator"]),
        )
        frames = decode_tracking_frames(
            source,
            source_start_pts=int(clip.analysis["source_start_pts"]),
            source_end_pts_exclusive=int(clip.analysis["source_end_pts_exclusive"]),
            expected_time_base=time_base,
        )
        return self.video_tracking_service.create_revision(
            project_id,
            annotation_id,
            expected_revision=expected_revision,
            expected_annotation_revision=expected_annotation_revision,
            source_video_fingerprint=source_fingerprint,
            frames=frames,
            correction=correction,
        )

    def annotation_preview_timing(
        self, project_id: str, clip_id: str
    ) -> dict[str, object]:
        clips = self.repositories.clips.load(project_id)
        clip = next((item for item in clips.clips if item.clip_id == clip_id), None)
        if clip is None:
            raise FileNotFoundError(f"clip not found: {clip_id}")
        frame_map_path = _clip_frame_map_path(clip)
        if frame_map_path is None:
            stored_jobs = tuple(
                QueueJob.from_dict(item)
                for item in self.repositories.jobs.load(project_id).jobs
            )
            _video_path, frame_map_path = _render_physical_inputs(clip, stored_jobs)
        frames = _load_authoritative_source_frames(clip, frame_map_path)
        time_base = _fraction_time_base(clip)
        first_pts = frames[0].pts
        return {
            "project_id": project_id,
            "clip_id": clip_id,
            "timestamp_authority": "source_decoded_frame_integer_pts",
            "time_base": {
                "numerator": time_base.numerator,
                "denominator": time_base.denominator,
            },
            "frames": [
                {
                    "source_pts": frame.pts,
                    "clip_time_sec": float((frame.pts - first_pts) * time_base),
                }
                for frame in frames
            ],
        }

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
            source = current.source_assets.get("video")
            path_value = source.get("path") if isinstance(source, Mapping) else None
            if not isinstance(path_value, str) or not path_value:
                raise ValueError("project source video is unavailable")
            spec = self.project_media_spec_probe(Path(path_value))
            existing = _project_media_binding(current)
            if existing is not None and existing[1] == spec:
                return current
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

    def cad_replacement_eligibility(self, project_id: str) -> dict[str, object]:
        """仅已保存且可校验的工作台输出证明项目坐标系已经打通。"""

        clips = self.repositories.clips.load(project_id)
        for clip in clips.clips:
            reference = _saved_workbench_reference(clip)
            if (
                reference is not None
                and _validate_workbench_immutable_output(
                    self.projects_root, project_id, clip, reference
                )
            ):
                return {"eligible": True, "reason": None}
        return {"eligible": False, "reason": "项目尚未完成坐标系标定"}

    def request_cad_replacement(
        self,
        project_id: str,
        upload: PublishedUpload,
        *,
        expected_revision: int,
        same_coordinate_system_confirmed: bool,
    ) -> EnqueueCadReplacementResult:
        """登记新版 CAD 候选并排队导入；成功前旧 CAD 始终保持活动。"""

        if upload.project_id != project_id or upload.asset_type != "cad":
            raise ValueError("CAD replacement upload belongs to another project")
        if not same_coordinate_system_confirmed:
            raise ValueError("必须确认新版 CAD 与当前项目使用相同坐标系")
        if not upload.path.is_file() or not upload.validation_report_path.is_file():
            raise FileNotFoundError("immutable replacement upload is unavailable")
        with self._state_guard(project_id):
            eligibility = self.cad_replacement_eligibility(project_id)
            if not eligibility["eligible"]:
                raise ValueError(str(eligibility["reason"]))
            project = self.repositories.project.load(project_id)
            if project.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=project.revision,
                )
            active_cad = project.source_assets.get("cad")
            if (
                isinstance(active_cad, Mapping)
                and active_cad.get("sha256") == upload.sha256
            ):
                raise ValueError("上传的 CAD 与当前活动图纸完全相同")
            previous = project.source_assets.get("_cad_replacement")
            previous_status = (
                previous.get("status") if isinstance(previous, Mapping) else None
            )
            if isinstance(previous, Mapping) and isinstance(
                previous.get("job_id"), str
            ):
                try:
                    previous_status = self.queue.get(str(previous["job_id"])).status
                except KeyError:
                    pass
            if previous_status in {
                "queued",
                "preparing",
                "running",
                "validating",
            }:
                raise ValueError("CAD 图纸替换任务正在进行")
            candidate = _upload_descriptor(upload)
            candidate_revision = (
                f"cad-upload:{upload.sha256[:16]}:{self._identity()}"
            )
            identity_payload = _cad_replacement_identity_payload(
                project_id=project_id,
                candidate_revision=candidate_revision,
                candidate=candidate,
            )
            job_id = self._identity()
            job = QueueJob(
                job_id=job_id,
                project_id=project_id,
                clip_id="__project__",
                job_type="cad_replacement",
                resource_class="light_compute",
                status="queued",
                stage="queued",
                priority=0,
                depends_on_job_ids=(),
                exclusive_key=f"cad-replacement:{project_id}",
                idempotency_key=_fingerprint(
                    {**identity_payload, "purpose": "idempotency"}
                ),
                input_revision=candidate_revision,
                input_fingerprint=_fingerprint(identity_payload),
                adapter_name=ANALYSIS_ADAPTER_NAME,
                adapter_version=ANALYSIS_ADAPTER_VERSION,
                output_revision=None,
                operation_id=self._identity(),
                attempts=(
                    AttemptRecord(
                        number=1,
                        directory=str(self._attempt_directory(project_id, job_id, 1)),
                    ),
                ),
            )
            batch = self.queue.prepare_submission_candidates((job,))
            submitted = batch.jobs[0]
            if submitted in batch.new_candidates:
                Path(submitted.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            assets = dict(project.source_assets)
            assets["_cad_replacement"] = {
                "status": "queued",
                "job_id": submitted.job_id,
                "candidate_revision": candidate_revision,
                "candidate": candidate,
                "same_coordinate_system_confirmed": True,
                "requested_at": self.now(),
                "progress": {
                    "stage": "queued",
                    "fraction": 0.0,
                    "message": "等待替换 CAD 图纸",
                },
                "error": None,
            }
            jobs_manifest = self.repositories.jobs.load(project_id)
            known_jobs = {str(item.get("job_id")): dict(item) for item in jobs_manifest.jobs}
            known_jobs[submitted.job_id] = submitted.to_dict()
            order = tuple(dict.fromkeys((*jobs_manifest.queue_order, submitted.job_id)))
            publication = publish_manifests(
                (
                    ManifestMutation(
                        repository=self.repositories.project,
                        project_id=project_id,
                        expected_revision=project.revision,
                        mutate=lambda value, _operation_id: replace(
                            value,
                            source_assets=assets,
                            updated_at=self.now(),
                        ),
                    ),
                    ManifestMutation(
                        repository=self.repositories.jobs,
                        project_id=project_id,
                        expected_revision=jobs_manifest.revision,
                        mutate=lambda value, _operation_id: replace(
                            value,
                            jobs=tuple(known_jobs[job_id] for job_id in order),
                            queue_order=order,
                            updated_at=self.now(),
                        ),
                    ),
                )
            )
            updated = next(
                item
                for item in publication.manifests
                if isinstance(item, ProjectManifest)
            )
            self.queue.commit_submission_candidates(batch.jobs)
            self.queue.acknowledge_publication(project_id)
            return EnqueueCadReplacementResult(
                job_id=submitted.job_id,
                project_revision=updated.revision,
                candidate_revision=candidate_revision,
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
            if adapter.name in {
                "srt_full_pose",
                "srt_fixed_track_visual_pose",
            }:
                georeference = _confirmed_cad_georeference(project.source_assets)
                if georeference is None:
                    skipped.append(clip.clip_id)
                    reasons[clip.clip_id] = (
                        "confirmed CAD georeference bound to the current CAD is "
                        f"required for {adapter.name}"
                    )
                    continue
                settings_key = adapter.name
                settings = clip.manual_definition.get(settings_key)
                if (
                    not isinstance(settings, Mapping)
                    or settings.get("horizontal_fov_deg") is None
                ):
                    skipped.append(clip.clip_id)
                    reasons[clip.clip_id] = (
                        f"horizontal FOV is required for {adapter.name}"
                    )
                    continue
                if adapter.name == "srt_fixed_track_visual_pose":
                    coverage = clip.analysis.get("srt_coverage")
                    if (
                        not isinstance(coverage, Mapping)
                        or float(coverage.get("trajectory_coverage", 0.0)) < 0.8
                    ):
                        skipped.append(clip.clip_id)
                        reasons[clip.clip_id] = (
                            "GPS and relative height coverage is insufficient "
                            "for the fixed SRT track"
                        )
                        continue
                frame_map_path = _clip_frame_map_path(clip)
                if frame_map_path is None or not frame_map_path.is_file():
                    reasons[clip.clip_id] = (
                        "exact frame map is missing; clip export will regenerate it"
                    )
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

    def preflight_project_merge(self, project_id: str) -> ConcatPreflight:
        """Validate that every logical clip has one exact current render."""

        return preflight_concat(self._project_concat_request(project_id))

    def _project_concat_request(
        self, project_id: str
    ) -> ConcatPreflightRequest:
        project = self.repositories.project.load(project_id)
        clips_manifest = self.repositories.clips.load(project_id)
        jobs_manifest = self.repositories.jobs.load(project_id)
        media_binding = _project_media_binding(project)
        video = project.source_assets.get("video")
        if media_binding is None:
            raise ValueError("project media specification is unavailable")
        if not isinstance(video, Mapping):
            raise ValueError("authoritative source video is unavailable")
        source_path = Path(str(video.get("path") or ""))
        source_sha256 = str(video.get("sha256") or "")
        if not source_path.is_file() or len(source_sha256) != 64:
            raise ValueError("authoritative source video identity is unavailable")

        stored_jobs = tuple(QueueJob.from_dict(item) for item in jobs_manifest.jobs)
        concat_clips: list[ConcatClip] = []
        candidates: dict[str, RenderCandidate] = {}
        source_frame_rows: list[tuple[int, int]] = []
        ordered_clips = tuple(
            sorted(
                clips_manifest.clips,
                key=lambda item: (
                    int(item.analysis.get("render_order", 0)),
                    int(item.analysis["source_start_pts"]),
                ),
            )
        )
        for expected_order, clip in enumerate(ordered_clips):
            render_job = next(
                (
                    item
                    for item in reversed(stored_jobs)
                    if item.job_type == "clip_render"
                    and item.clip_id == clip.clip_id
                    and _has_exact_success_proof(item)
                    and self._current_input_fingerprint(item) == item.input_fingerprint
                    and self._exact_render_owner_record(item) is not None
                ),
                None,
            )
            if render_job is None:
                continue
            video_path = Path(str(render_job.published_outputs.get("video") or ""))
            frame_map_path = Path(
                str(render_job.published_outputs.get("frame_map") or "")
            )
            frame_map = json.loads(frame_map_path.read_text(encoding="utf-8"))
            if not isinstance(frame_map, Mapping):
                raise ValueError("render frame map must be an object")
            frames = frame_map.get("frames")
            if not isinstance(frames, list) or not frames:
                raise ValueError("render frame map contains no source frames")
            source_frame_rows.extend(
                (
                    int(item["source_decoded_frame_ordinal"]),
                    int(item["source_pts"]),
                )
                for item in frames
                if isinstance(item, Mapping)
            )
            time_base = _fraction_time_base(clip)
            concat_clip = ConcatClip(
                clip_id=clip.clip_id,
                render_order=expected_order,
                analysis_revision=clip.analysis_revision,
                resolved_workflow=str(clip.resolved_workflow),
                source_start_pts=int(clip.analysis["source_start_pts"]),
                source_end_pts_exclusive=int(
                    clip.analysis["source_end_pts_exclusive"]
                ),
                source_time_base=time_base,
                authoritative_frame_map=frame_map,
                current_render_input_fingerprint=render_job.input_fingerprint,
            )
            proof = dict(render_job.validation_proof or {})
            output_pts = tuple(int(value) for value in proof.get("output_pts", ()))
            media_spec_revision, media_spec = media_binding
            candidate_media = ProbedMedia(
                video=VideoMediaInfo(
                    **{
                        key: getattr(media_spec, key)
                        for key in (
                            "width", "height", "display_orientation_baked",
                            "sample_aspect_ratio", "pixel_format", "codec_name",
                            "profile", "time_base", "color_range", "color_space",
                            "color_transfer", "color_primaries", "nominal_frame_rate",
                        )
                    },
                    frame_pts=output_pts,
                    frame_duration_pts=tuple(None for _ in output_pts),
                ),
                audio=None,
                format_duration_sec=None,
            )
            concat_clips.append(concat_clip)
            candidates[clip.clip_id] = RenderCandidate(
                project_id=project_id,
                clip_id=clip.clip_id,
                workflow=str(clip.resolved_workflow),
                exact_validated=True,
                input_fingerprint=render_job.input_fingerprint,
                output_revision=str(render_job.output_revision),
                output_fingerprint=str(render_job.output_fingerprint),
                proof_fingerprint=sha256(
                    json.dumps(
                        proof, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
                video_sha256=str(proof.get("video_sha256") or ""),
                frame_map_sha256=str(proof.get("frame_map_sha256") or ""),
                publication_operation_id=str(
                    render_job.publication_operation_id or ""
                ),
                video_path=str(video_path),
                frame_map_path=str(frame_map_path),
                media=candidate_media,
                render_frame_map=frame_map,
            )

        if not clips_manifest.clips:
            raise ValueError("project contains no logical clips")
        if not source_frame_rows:
            # Build a syntactically valid request so preflight reports every clip as
            # blocked without silently probing or regenerating source timestamps.
            raise ValueError("project contains no exact current rendered frames")
        if len(source_frame_rows) != len({ordinal for ordinal, _pts in source_frame_rows}):
            raise ValueError("render frame maps contain duplicate source frames")
        source_frame_rows.sort()
        last_end = int(ordered_clips[-1].analysis["source_end_pts_exclusive"])
        source_frames = tuple(
            DecodedFrameTimestamp(
                ordinal=ordinal,
                pts=pts,
                duration_pts=(
                    source_frame_rows[index + 1][1] - pts
                    if index + 1 < len(source_frame_rows)
                    else last_end - pts
                ),
                timestamp_source="pts",
            )
            for index, (ordinal, pts) in enumerate(source_frame_rows)
        )
        source_index = DecodedFrameIndex(
            _fraction_time_base(ordered_clips[0]), source_frames
        )
        return ConcatPreflightRequest(
            project_id=project_id,
            project_revision=project.revision,
            clips_revision=clips_manifest.revision,
            source_frame_index=source_index,
            clips=tuple(concat_clips),
            render_candidates=candidates,
            fallback_confirmations={},
            fallback_artifacts={},
            source_asset_fingerprint=source_sha256,
            project_media_spec_revision=media_spec_revision,
            project_media_spec=media_spec,
            original_video_path=str(source_path),
        )

    def _validate_concat_execution(self, inputs, execution) -> AdapterResult:
        proof = dict(
            validate_concat_outputs(
                execution,
                project_media_spec=inputs.project_media_spec,
                media_probe=self.media_probe,
                source_media_probe=self.media_probe,
            )
        )
        source_media = self.media_probe(inputs.source_video_path)
        proof.update(
            {
                "audio_policy": (
                    "original_video" if source_media.audio is not None else "video_only"
                ),
                "source_asset_sha256": execution.source_asset_fingerprint,
                "segment_video_sha256": {
                    item.clip_id: item.source_video_sha256
                    for item in execution.segments
                },
                "segment_frame_map_sha256": {
                    item.clip_id: item.source_frame_map_sha256
                    for item in execution.segments
                },
                "video_sha256": sha256(execution.final_output.read_bytes()).hexdigest(),
                "frame_map_sha256": sha256(
                    execution.final_frame_map_path.read_bytes()
                ).hexdigest(),
            }
        )
        fingerprint = sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return AdapterResult.success(
            output_revision=f"concat-{fingerprint[:16]}",
            output_fingerprint=fingerprint,
            outputs={
                "video": str(execution.final_output),
                "frame_map": str(execution.final_frame_map_path),
            },
            validation_proof=proof,
        )

    def published_render_video_path(
        self,
        project_id: str,
        clip_id: str,
        output_revision: str,
    ) -> Path:
        """Resolve one immutable, service-owned published render video."""

        if not all(
            is_safe_stable_id(value)
            for value in (project_id, clip_id, output_revision)
        ):
            raise ValueError("invalid published render identity")
        expected = (
            self.projects_root
            / project_id
            / "render_outputs"
            / clip_id
            / output_revision
            / "rendered.mp4"
        ).resolve()
        record = next(
            (
                item
                for item in reversed(
                    self.repositories.render.load(project_id).clip_renders
                )
                if item.get("clip_id") == clip_id
                and item.get("output_revision") == output_revision
                and item.get("status") in {"success", "stale_input"}
            ),
            None,
        )
        outputs = record.get("outputs") if isinstance(record, Mapping) else None
        raw_video = outputs.get("video") if isinstance(outputs, Mapping) else None
        if not isinstance(raw_video, str) or Path(raw_video).resolve() != expected:
            raise FileNotFoundError("published render video is unavailable")
        if not expected.is_file():
            raise FileNotFoundError("published render video is unavailable")
        return expected

    def enqueue_project_merge(
        self,
        project_id: str,
        *,
        expected_jobs_revision: int | None = None,
    ) -> EnqueueMergeResult:
        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            request = self._project_concat_request(project_id)
            preflight = preflight_concat(request)
            plan = build_concat_plan(request)
            dependencies = tuple(
                candidate
                for candidate in (
                    next(
                        (
                            job
                            for job in reversed(tuple(self.queue.jobs()))
                            if job.project_id == project_id
                            and job.job_type == "clip_render"
                            and job.clip_id == entry.clip_id
                            and job.status == "success"
                            and job.output_revision == entry.input_output_revision
                        ),
                        None,
                    )
                    for entry in plan.entries
                )
                if candidate is not None
            )
            if len(dependencies) != len(plan.entries):
                raise RuntimeError("merge render dependencies changed after preflight")
            identity_payload = {
                "plan": plan.to_dict(),
                "render_job_ids": [job.job_id for job in dependencies],
                "adapter_name": self.concat_adapter.name,
                "adapter_version": self.concat_adapter.version,
            }
            fingerprint = _fingerprint(identity_payload)
            job_id = self._identity()
            attempt_dir = self._attempt_directory(project_id, job_id, 1)
            job = QueueJob(
                job_id=job_id,
                project_id=project_id,
                clip_id="project-output",
                job_type="project_merge",
                resource_class="media_io",
                status="queued",
                stage="queued",
                priority=0,
                depends_on_job_ids=tuple(job.job_id for job in dependencies),
                exclusive_key=f"merge:{project_id}",
                idempotency_key=_fingerprint(
                    {**identity_payload, "purpose": "idempotency"}
                ),
                input_revision=f"{plan.project_revision}:{plan.clips_revision}",
                input_fingerprint=fingerprint,
                adapter_name=self.concat_adapter.name,
                adapter_version=self.concat_adapter.version,
                output_revision=None,
                operation_id=self._identity(),
                attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
            )
            submitted = self.queue.submit(job)
            if submitted.job_id == job.job_id:
                attempt_dir.mkdir(parents=True, exist_ok=False)
            self._publish_queue_locked(project_id)
            return EnqueueMergeResult(job_id=submitted.job_id, preflight=preflight)

    def published_merge_video_path(self, project_id: str) -> Path:
        jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(project_id).jobs
        )
        job = next(
            (
                item
                for item in reversed(jobs)
                if item.job_type == "project_merge"
                and _has_exact_success_proof(item)
                and self._current_input_fingerprint(item) == item.input_fingerprint
            ),
            None,
        )
        if job is None:
            raise FileNotFoundError("current merged output is unavailable")
        path = Path(str(job.published_outputs.get("video") or "")).resolve(strict=True)
        attempt = Path(job.attempts[-1].directory).resolve(strict=True)
        path.relative_to(attempt)
        if not path.is_file():
            raise FileNotFoundError("current merged output is unavailable")
        return path

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
                    and self._current_input_fingerprint(existing)
                    == existing.input_fingerprint
                ):
                    if existing.status in {
                        "queued",
                        "preparing",
                        "running",
                        "validating",
                        "success",
                    }:
                        return existing
                    if existing.status in {
                        "failed",
                        "interrupted",
                        "cancelled",
                        "stale_input",
                        "superseded",
                    }:
                        return self.retry_job(
                            project_id,
                            existing.job_id,
                            expected_jobs_revision=expected_jobs_revision,
                        )
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

    def enqueue_scene_bridge(
        self,
        project_id: str,
        source_clip_id: str,
        *,
        direction: str,
        expected_jobs_revision: int,
        expected_clips_revision: int,
    ) -> EnqueueSceneBridgeResult:
        if direction not in {"up", "down"}:
            raise ValueError("scene bridge direction must be up or down")
        with self._state_guard(project_id):
            self._require_jobs_revision_locked(project_id, expected_jobs_revision)
            clips_manifest = self.repositories.clips.load(project_id)
            if clips_manifest.revision != expected_clips_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_clips_revision,
                    current_revision=clips_manifest.revision,
                )
            project = self.repositories.project.load(project_id)
            source = next(
                (clip for clip in clips_manifest.clips if clip.clip_id == source_clip_id),
                None,
            )
            if source is None:
                raise KeyError(f"unknown clip ID: {source_clip_id}")
            target = _same_scene_adjacent_clip(clips_manifest.clips, source, direction)
            if target is None:
                raise ValueError("same scene has no adjacent target clip")
            if source.resolved_workflow != "sfm_only" or target.resolved_workflow != "sfm_only":
                raise ValueError("scene bridge supports only SfM clips")
            source_workbench = _saved_workbench_reference(source)
            if (
                source_workbench is None
                or source_workbench.value.get("status") != "saved"
                or not _validate_workbench_immutable_output(
                    self.projects_root, project_id, source, source_workbench
                )
            ):
                raise ValueError("source clip has no validated saved route")
            target_workbench = next(
                (
                    reference
                    for reference in reversed(target.references)
                    if reference.owner == "clips"
                    and reference.key == f"workbench:{target.clip_id}"
                    and reference.value.get("status")
                    in {"editing", "pending_save", "saved"}
                ),
                None,
            )
            if target_workbench is not None and _workbench_reference_blocks_scene_bridge(
                target_workbench, self.now()
            ):
                raise ValueError("target clip already has a workbench result")
            stored_jobs = tuple(
                QueueJob.from_dict(item)
                for item in self.repositories.jobs.load(project_id).jobs
            )
            target_trajectory = self._current_trajectory_for_render(
                target, stored_jobs
            )
            if _has_recoverable_saved_workbench_output(
                self.projects_root, project_id, target, target_trajectory
            ):
                raise ValueError(
                    "target clip has a recoverable saved workbench output"
                )
            jobs_by_id = {job.job_id: job for job in stored_jobs}
            active_bridge = None
            for reference in reversed(target.references):
                if (
                    reference.owner != "jobs"
                    or reference.value.get("reference_type") != "scene_bridge"
                    or reference.value.get("target_clip_id") != target.clip_id
                ):
                    continue
                bridge_job = jobs_by_id.get(str(reference.value.get("job_id")))
                if (
                    bridge_job is None
                    or not _has_exact_success_proof(bridge_job)
                    or bridge_job.output_revision
                    != reference.value.get("bridge_revision")
                    or bridge_job.output_fingerprint
                    != reference.value.get("output_fingerprint")
                    or not _validate_scene_bridge_reference(
                        self.projects_root, project_id, target, reference
                    )
                ):
                    continue
                active_bridge = reference
                break
            if active_bridge is not None:
                raise ValueError(
                    "target clip has a scene bridge awaiting route refinement"
                )
            source_frame_map = _core_frame_map_identity(source, stored_jobs)
            target_frame_map = _core_frame_map_identity(target, stored_jobs)
            source_trajectory = self._current_trajectory_for_render(
                source, stored_jobs
            )
            if source_trajectory is None or not _has_precomputed_solve_outputs(
                source_trajectory
            ):
                raise ValueError(
                    "source clip requires current precomputed overlap SfM; rerun trajectory solve"
                )
            if target_trajectory is None or not _has_precomputed_solve_outputs(
                target_trajectory
            ):
                raise ValueError(
                    "target clip requires current precomputed overlap SfM; rerun trajectory solve"
                )
            if target_trajectory.job_id not in {
                job.job_id for job in self.queue.jobs()
            }:
                raise RuntimeError("current target trajectory is not in the live queue")
            dependency_ids = (target_trajectory.job_id,)
            semantic_identity = _scene_bridge_identity_payload(
                project=project,
                source=source,
                target=target,
                source_workbench=source_workbench,
                source_trajectory=source_trajectory,
                target_trajectory=target_trajectory,
                direction=direction,
                source_core_frame_map=source_frame_map,
                target_core_frame_map=target_frame_map,
            )
            input_fingerprint = _fingerprint(semantic_identity)
            idempotency_key = _fingerprint(
                {**semantic_identity, "purpose": "idempotency"}
            )

            def bridge_request(operation_id: str) -> dict[str, object]:
                return {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "identity": semantic_identity,
                    "runner_identity": {
                        **semantic_identity,
                        "schema_version": 1,
                        "algorithm_version": BRIDGE_ALGORITHM_VERSION,
                        "project_id": project_id,
                        "source_clip_id": source.clip_id,
                        "target_clip_id": target.clip_id,
                        "direction": direction,
                        "operation_id": operation_id,
                    },
                    "source_trajectory_job_id": source_trajectory.job_id,
                    "target_trajectory_job_id": target_trajectory.job_id,
                }

            def bridge_request_path(operation_id: str) -> Path:
                return (
                    self.projects_root
                    / project_id
                    / "scene_bridge_requests"
                    / f"{operation_id}.json"
                )

            existing = next(
                (
                    job
                    for job in reversed(self.queue.jobs())
                    if job.project_id == project_id
                    and job.clip_id == target.clip_id
                    and job.job_type == "scene_bridge"
                    and job.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.status in {
                    "failed",
                    "interrupted",
                    "cancelled",
                    "stale_input",
                    "superseded",
                }:
                    try:
                        request_operation_id = str(
                            self._load_scene_bridge_request(existing)["operation_id"]
                        )
                    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                        request_operation_id = str(
                            existing.submission_operation_id or existing.operation_id
                        )
                    _atomic_write_json_file(
                        bridge_request_path(request_operation_id),
                        bridge_request(request_operation_id),
                    )
                    existing = self.retry_job(
                        project_id,
                        existing.job_id,
                        expected_jobs_revision=expected_jobs_revision,
                    )
                return EnqueueSceneBridgeResult(
                    existing, source.clip_id, target.clip_id, direction
                )
            operation_id = self._identity()
            _atomic_write_json_file(
                bridge_request_path(operation_id), bridge_request(operation_id)
            )
            job_id = self._identity()
            attempt_dir = self._attempt_directory(project_id, job_id, 1)
            bridge = QueueJob(
                job_id=job_id,
                project_id=project_id,
                clip_id=target.clip_id,
                job_type="scene_bridge",
                resource_class="heavy_compute",
                status="queued",
                stage="queued",
                priority=0,
                depends_on_job_ids=dependency_ids,
                exclusive_key=f"scene_bridge:{project_id}:{target.clip_id}",
                idempotency_key=idempotency_key,
                input_revision=target.analysis_revision,
                input_fingerprint=input_fingerprint,
                adapter_name="scene_bridge",
                adapter_version="1",
                output_revision=None,
                operation_id=operation_id,
                submission_operation_id=operation_id,
                attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
            )
            submitted = self.queue.submit(bridge)
            if submitted.job_id == bridge.job_id:
                Path(submitted.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            self._publish_queue_locked(project_id)
            return EnqueueSceneBridgeResult(
                submitted, source.clip_id, target.clip_id, direction
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

        allowed = {
            "sfm_only",
            "srt_sfm_fused",
            "srt_fixed_track_visual_pose",
            "srt_full_pose",
            "pure_rotation",
        }
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

    def enqueue_cad_georeference_candidates(
        self,
        project_id: str,
        *,
        expected_revision: int,
        central_meridian_deg: object | None = None,
        limit: int = 6,
    ) -> QueueJob:
        """Queue one fingerprinted, project-level CRS candidate operation."""

        with self._state_guard(project_id):
            project = self.repositories.project.load(project_id)
            if project.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=project.revision,
                )
            request_assets = _complete_cad_georeference_assets(
                project.source_assets,
                self.repositories.clips.load(project_id),
            )
            request = _cad_georeference_candidate_request(
                project_id,
                request_assets,
                central_meridian_deg=central_meridian_deg,
                limit=limit,
            )
            input_fingerprint = _fingerprint(request)
            request = {**request, "input_fingerprint": input_fingerprint}
            idempotency_key = _fingerprint(
                {
                    "purpose": "cad_georeference_candidates",
                    "input_fingerprint": input_fingerprint,
                }
            )
            existing = next(
                (
                    item
                    for item in reversed(self.queue.jobs())
                    if item.project_id == project_id
                    and item.job_type == "cad_georeference_candidates"
                    and item.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return existing
            job_id = self._identity()
            attempt_dir = self._attempt_directory(project_id, job_id, 1)
            attempt_dir.mkdir(parents=True, exist_ok=False)
            _atomic_write_json_file(
                attempt_dir / "cad_georeference_candidate_request.json",
                request,
            )
            job = QueueJob(
                job_id=job_id,
                project_id=project_id,
                clip_id="project-coordinate-system",
                job_type="cad_georeference_candidates",
                resource_class="light_compute",
                status="queued",
                stage="queued",
                priority=0,
                depends_on_job_ids=(),
                exclusive_key=f"cad-georeference:{project_id}",
                idempotency_key=idempotency_key,
                input_revision="cad-georeference-candidates-v1",
                input_fingerprint=input_fingerprint,
                adapter_name=CAD_GEOREFERENCE_CANDIDATE_ADAPTER_NAME,
                adapter_version=CAD_GEOREFERENCE_CANDIDATE_ADAPTER_VERSION,
                output_revision=None,
                operation_id=self._identity(),
                attempts=(AttemptRecord(number=1, directory=str(attempt_dir)),),
            )
            try:
                submitted = self.queue.submit(job)
                self._publish_queue_locked(project_id)
            except Exception:
                shutil.rmtree(attempt_dir, ignore_errors=True)
                raise
            return submitted

    def cad_georeference_candidate_status(
        self, project_id: str
    ) -> Mapping[str, object]:
        candidates = tuple(
            item
            for item in self.queue.jobs()
            if item.project_id == project_id
            and item.job_type == "cad_georeference_candidates"
        )
        if not candidates:
            return {
                "job_type": "cad_georeference_candidates",
                "status": "not_started",
                "stale": False,
                "candidates": [],
            }
        job = candidates[-1]
        request = self._load_cad_georeference_candidate_request(job)
        authoritative = self._current_input_fingerprint(job)
        stale = authoritative != job.input_fingerprint
        result_candidates: list[object] = []
        if _has_exact_success_proof(job) and not stale:
            output = self._read_cad_georeference_candidate_output(job)
            raw_candidates = output.get("candidates")
            if isinstance(raw_candidates, list):
                result_candidates = list(raw_candidates)
        progress = None if job.progress is None else dict(job.progress)
        if (
            job.status == "success"
            and progress is not None
            and progress.get("stage") == "complete"
        ):
            progress["message"] = "坐标系候选生成完成"
        return {
            "job_id": job.job_id,
            "job_type": job.job_type,
            "status": (
                "stale_input" if stale and job.status == "success" else job.status
            ),
            "stage": (
                "stale_input" if stale and job.status == "success" else job.stage
            ),
            "progress": progress,
            "error": job.error,
            "stale": stale,
            "input_fingerprint": job.input_fingerprint,
            "requested_central_meridian_deg": request.get(
                "central_meridian_deg"
            ),
            "candidates": result_candidates,
        }

    def _load_cad_georeference_candidate_request(
        self, job: QueueJob
    ) -> Mapping[str, object]:
        for attempt in reversed(job.attempts):
            path = (
                Path(attempt.directory)
                / "cad_georeference_candidate_request.json"
            )
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, Mapping)
                and payload.get("input_fingerprint") == job.input_fingerprint
                and payload.get("algorithm_version")
                == CAD_GEOREFERENCE_CANDIDATE_ADAPTER_VERSION
            ):
                return payload
        raise FileNotFoundError("CAD georeference candidate request is unavailable")

    def _read_cad_georeference_candidate_output(
        self, job: QueueJob
    ) -> Mapping[str, object]:
        raw_path = job.published_outputs.get("cad_georeference_candidates")
        if raw_path is None:
            raise FileNotFoundError("CAD georeference candidate output is unavailable")
        path = Path(raw_path).resolve()
        attempt = Path(job.attempts[-1].directory).resolve()
        if not path.is_relative_to(attempt) or not path.is_file():
            raise FileNotFoundError("CAD georeference candidate output is unsafe")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise ValueError("CAD georeference candidate output must be an object")
        if payload.get("input_fingerprint") != job.input_fingerprint:
            raise ValueError("CAD georeference candidate output fingerprint mismatch")
        if not isinstance(payload.get("candidates"), list):
            raise ValueError("CAD georeference candidate output has no candidate list")
        return payload

    def _validate_current_cad_georeference_candidate(
        self,
        project_id: str,
        candidate: Mapping[str, object],
        *,
        candidate_job_id: str | None,
        candidate_input_fingerprint: str | None,
    ) -> None:
        if not candidate_job_id or not candidate_input_fingerprint:
            raise ValueError(
                "candidate_job_id and candidate_input_fingerprint are required"
            )
        latest = next(
            (
                item
                for item in reversed(self.queue.jobs())
                if item.project_id == project_id
                and item.job_type == "cad_georeference_candidates"
            ),
            None,
        )
        if latest is None or latest.job_id != candidate_job_id:
            raise ValueError("CAD georeference candidate job is not current")
        if (
            latest.input_fingerprint != candidate_input_fingerprint
            or not _has_exact_success_proof(latest)
            or self._current_input_fingerprint(latest) != latest.input_fingerprint
        ):
            raise ValueError("CAD georeference candidate result is stale or invalid")
        output = self._read_cad_georeference_candidate_output(latest)
        allowed = output["candidates"]
        candidate_fingerprint = _fingerprint(dict(candidate))
        if not any(
            isinstance(item, Mapping)
            and _fingerprint(dict(item)) == candidate_fingerprint
            for item in allowed  # type: ignore[union-attr]
        ):
            raise ValueError("candidate does not belong to the current result")

    def _prepare_cad_georeference_candidate_plan(
        self, job: QueueJob
    ) -> JobExecutionPlan:
        request = self._load_cad_georeference_candidate_request(job)
        attempt = Path(job.attempts[-1].directory)
        request_path = attempt / "cad_georeference_candidate_request.json"
        if not request_path.is_file():
            _atomic_write_json_file(request_path, request)
        output_path = attempt / "cad_georeference_candidates.json"
        progress_path = attempt / "adapter_progress.json"
        command = (
            sys.executable,
            "-m",
            "cadscene.cli.build_cad_georeference_candidates",
            "--request",
            str(request_path),
            "--output",
            str(output_path),
            "--progress-file",
            str(progress_path),
        )
        return JobExecutionPlan(
            commands=(command,),
            validate=lambda: self._validate_cad_georeference_candidate_outputs(
                job
            ),
        )

    def _validate_cad_georeference_candidate_outputs(
        self, job: QueueJob
    ) -> AdapterResult:
        output = (
            Path(job.attempts[-1].directory)
            / "cad_georeference_candidates.json"
        )
        try:
            payload = json.loads(output.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, Mapping):
                raise ValueError("candidate output root must be an object")
            if payload.get("input_fingerprint") != job.input_fingerprint:
                raise ValueError("candidate output input fingerprint mismatch")
            if payload.get("algorithm_version") != job.adapter_version:
                raise ValueError("candidate output algorithm version mismatch")
            candidates = payload.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                raise ValueError("candidate output contains no candidates")
            for item in candidates:
                if not isinstance(item, Mapping):
                    raise ValueError("candidate output contains an invalid item")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return AdapterResult.failed(str(exc))
        digest = sha256(output.read_bytes()).hexdigest()
        return AdapterResult.success(
            output_revision=f"cad-georef-candidates:{digest[:16]}",
            output_fingerprint=digest,
            outputs={"cad_georeference_candidates": str(output)},
            validation_proof={
                "input_fingerprint": job.input_fingerprint,
                "candidate_count": len(candidates),
            },
        )

    def recommend_cad_georeference(
        self,
        project_id: str,
        *,
        limit: int = 6,
        central_meridian_deg: object | None = None,
        progress_callback: Callable[[str, str, float | None], None] | None = None,
    ) -> tuple[CrsCandidate, ...]:
        """Rank CGCS2000 Gauss-Kruger candidates without confirming one."""

        project = self.repositories.project.load(project_id)
        request_assets = _complete_cad_georeference_assets(
            project.source_assets,
            self.repositories.clips.load(project_id),
        )
        srt_path = _asset_path(request_assets, "srt")
        if srt_path is None or not srt_path.is_file():
            raise FileNotFoundError("physical SRT is required for CAD georeference")
        records = load_srt_records(srt_path)
        gps = tuple(
            (float(record.longitude), float(record.latitude))
            for record in records
            if record.longitude is not None and record.latitude is not None
        )
        if not gps:
            raise ValueError("SRT contains no usable WGS84 longitude/latitude samples")
        return recommend_cgcs2000_candidates(
            [item[0] for item in gps],
            [item[1] for item in gps],
            _active_cad_coordinate_bbox(request_assets),
            limit=limit,
            central_meridian_deg=parse_central_meridian(central_meridian_deg),
            progress_callback=progress_callback,
        )

    def confirm_cad_georeference(
        self,
        project_id: str,
        candidate: Mapping[str, object],
        *,
        expected_revision: int,
        candidate_job_id: str | None = None,
        candidate_input_fingerprint: str | None = None,
    ) -> ProjectManifest:
        """Persist an explicit projection choice bound to the active CAD asset."""

        project = self.repositories.project.load(project_id)
        if project.revision != expected_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected_revision,
                current_revision=project.revision,
            )
        if candidate_job_id is not None or candidate_input_fingerprint is not None:
            self._validate_current_cad_georeference_candidate(
                project_id,
                candidate,
                candidate_job_id=candidate_job_id,
                candidate_input_fingerprint=candidate_input_fingerprint,
            )
        cad_fingerprint = _active_cad_asset_fingerprint(project.source_assets)
        if cad_fingerprint is None:
            raise ValueError("active CAD asset identity is unavailable")
        evidence = candidate.get("evidence")
        validation = (
            {
                str(key): value
                for key, value in evidence.items()
                if key not in {"cad_bbox_raw", "trajectory_polyline_raw"}
            }
            if isinstance(evidence, Mapping)
            else {}
        )
        score = float(candidate.get("score", 0.0))
        confidence = min(1.0, max(0.0, score / 120.0))
        config = CadGeoreference.from_dict(
            {
                "schema_version": 1,
                "horizontal_datum": "CGCS2000",
                "projection_family": "gauss_kruger",
                "zone_width_deg": candidate["zone_width_deg"],
                "central_meridian_deg": candidate["central_meridian_deg"],
                "epsg": candidate["epsg"],
                "projected_axis_order": "easting_northing",
                "cad_axis_mapping": candidate["cad_axis_mapping"],
                "zone_prefix": candidate["zone_prefix"],
                "linear_unit": "metre",
                "source": "user_confirmed_candidate",
                "confirmed": True,
                "confidence": confidence,
                "validation": validation,
                "crs_source": candidate.get(
                    "crs_source",
                    "custom" if candidate.get("epsg") is None else "epsg",
                ),
                "latitude_of_origin_deg": candidate.get(
                    "latitude_of_origin_deg", 0.0
                ),
                "scale_factor": candidate.get("scale_factor", 1.0),
                "false_easting_m": candidate.get("false_easting_m", 500_000.0),
                "false_northing_m": candidate.get("false_northing_m", 0.0),
                "ellipsoid": candidate.get("ellipsoid", "GRS80"),
            }
        )
        payload = {
            **config.to_dict(),
            "cad_asset_fingerprint": cad_fingerprint,
        }
        payload["revision"] = f"cad-georef-{_fingerprint(payload)[:16]}"
        return self.repositories.project.update(
            project_id,
            expected_revision=expected_revision,
            mutate=lambda value: replace(
                value,
                updated_at=self.now(),
                source_assets={
                    **value.source_assets,
                    "_cad_georeference": payload,
                },
            ),
        )

    def update_srt_full_pose_settings(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int,
        horizontal_fov_deg: float,
        cad_z_offset_m: float = 0.0,
        attitude_profile: str = "dji_absolute_ned",
    ) -> ClipsManifest:
        """Store the deliberately small user contract for DJI full-pose SRT."""

        fov = float(horizontal_fov_deg)
        z_offset = float(cad_z_offset_m)
        if not isfinite(fov) or not 1.0 < fov < 179.0:
            raise ValueError("horizontal_fov_deg must be finite and inside (1, 179)")
        if not isfinite(z_offset):
            raise ValueError("cad_z_offset_m must be finite")
        if attitude_profile != "dji_absolute_ned":
            raise ValueError("unsupported attitude_profile")
        settings = {
            "horizontal_fov_deg": fov,
            "cad_z_offset_m": z_offset,
            "attitude_profile": attitude_profile,
        }
        with self._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            if not any(item.clip_id == clip_id for item in current.clips):
                raise KeyError(f"unknown clip ID: {clip_id}")
            return self.repositories.clips.update(
                project_id,
                expected_revision=expected_revision,
                mutate=lambda value: replace(
                    value,
                    updated_at=self.now(),
                    clips=tuple(
                        replace(
                            item,
                            manual_definition={
                                **item.manual_definition,
                                "srt_full_pose": settings,
                            },
                        )
                        if item.clip_id == clip_id
                        else item
                        for item in value.clips
                    ),
                ),
            )

    def update_srt_fixed_track_visual_pose_settings(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int,
        horizontal_fov_deg: float,
        route_offset_xyz_m: Sequence[float] = (0.0, 0.0, 0.0),
    ) -> ClipsManifest:
        """Store FOV and the only allowed whole-route XYZ position adjustment."""

        fov = float(horizontal_fov_deg)
        if not isfinite(fov) or not 1.0 < fov < 179.0:
            raise ValueError("horizontal_fov_deg must be finite and inside (1, 179)")
        if (
            not isinstance(route_offset_xyz_m, Sequence)
            or isinstance(route_offset_xyz_m, (str, bytes))
            or len(route_offset_xyz_m) != 3
        ):
            raise ValueError("route_offset_xyz_m must contain exactly three values")
        offset = [float(value) for value in route_offset_xyz_m]
        if not all(isfinite(value) for value in offset):
            raise ValueError("route_offset_xyz_m must contain finite values")
        settings = {
            "schema_version": 1,
            "horizontal_fov_deg": fov,
            "route_offset_xyz_m": offset,
        }
        with self._state_guard(project_id):
            current = self.repositories.clips.load(project_id)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            selected = next(
                (item for item in current.clips if item.clip_id == clip_id),
                None,
            )
            if selected is None:
                raise KeyError(f"unknown clip ID: {clip_id}")
            if selected.manual_definition.get(
                "srt_fixed_track_visual_pose"
            ) == settings:
                return current

            def update_clip(item: ClipDefinition) -> ClipDefinition:
                if item.clip_id != clip_id:
                    return item
                references = tuple(
                    replace(
                        reference,
                        value={
                            **reference.value,
                            "previous_status": reference.value.get("status"),
                            "status": "stale",
                            "stale_reason": (
                                "fixed_track_visual_pose_settings_changed"
                            ),
                        },
                    )
                    for reference in item.references
                )
                return replace(
                    item,
                    manual_definition={
                        **item.manual_definition,
                        "srt_fixed_track_visual_pose": settings,
                    },
                    references=references,
                )

            return self.repositories.clips.update(
                project_id,
                expected_revision=expected_revision,
                mutate=lambda value: replace(
                    value,
                    updated_at=self.now(),
                    clips=tuple(update_clip(item) for item in value.clips),
                ),
            )

    def cad_georeference_snapshot(self, project_id: str) -> Mapping[str, object]:
        return _cad_georeference_snapshot(
            self.repositories.project.load(project_id).source_assets
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
                or (
                    by_id[clip_id].resolved_workflow
                    in {
                        "sfm_only",
                        "srt_full_pose",
                        "srt_fixed_track_visual_pose",
                    }
                    and (
                        _clip_frame_map_path(by_id[clip_id]) is None
                        or not _clip_frame_map_path(by_id[clip_id]).is_file()
                    )
                )
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
        stored_jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(project_id).jobs
        )
        source_frame_index: DecodedFrameIndex | None = None
        solve_export_dependencies: dict[str, tuple[str, ...]] = {}
        for clip_id in accepted_ids:
            solve_clip = by_id[clip_id]
            if not _uses_scene_solve_export(solve_clip, clips_manifest.clips):
                continue
            if source_frame_index is None:
                source_frame_index = _project_source_frame_index(
                    clips_manifest.clips, stored_jobs
                )
            solve_interval = derive_scene_solve_interval(
                clips=clips_manifest.clips,
                clip_id=solve_clip.clip_id,
                source_frame_index=source_frame_index,
            )
            export = self._new_solve_export_job(
                project_id,
                solve_clip,
                solve_interval=solve_interval,
                project_assets=project.source_assets,
                project_revision=project.revision,
                clips_revision=clips_manifest.revision,
            )
            submitted_export = self.queue.submit(export)
            if submitted_export.job_id == export.job_id:
                Path(submitted_export.attempts[-1].directory).mkdir(
                    parents=True, exist_ok=False
                )
            solve_export_dependencies[solve_clip.clip_id] = (
                submitted_export.job_id,
            )
        for clip_id in accepted_ids:
            clip = by_id[clip_id]
            adapter = self.adapters.for_workflow(str(clip.resolved_workflow))
            physical_clip = _clip_output_path(clip)
            dependency_ids = (
                *(
                    export_dependencies[clip.clip_id]
                    if clip.clip_id in export_dependencies
                    else ()
                ),
                *solve_export_dependencies.get(clip.clip_id, ()),
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
            elif submitted_solve.status == "cancelled":
                number = len(submitted_solve.attempts) + 1
                directory = self._attempt_directory(
                    project_id, submitted_solve.job_id, number
                )
                directory.mkdir(parents=True, exist_ok=False)
                try:
                    submitted_solve = self.queue.retry(
                        submitted_solve.job_id,
                        AttemptRecord(number=number, directory=str(directory)),
                    )
                except Exception:
                    directory.rmdir()
                    raise
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
            except _SceneBridgePublicationPending as pending:
                return self._recover_scene_bridge_publication(pending)

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
            if current.job_type == "cad_replacement":
                return self._finish_cad_replacement_job_locked(
                    current,
                    result,
                    authoritative_fingerprint=authoritative_fingerprint,
                    lease=lease,
                )
            if current.job_type == "clip_render":
                return self._finish_render_job_locked(
                    current,
                    result,
                    authoritative_fingerprint=authoritative_fingerprint,
                    lease=lease,
                )
            if current.job_type == "scene_bridge":
                return self._finish_scene_bridge_job_locked(
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
                        validation_proof=result.validation_proof,
                        **lease,
                    )
            self._publish_queue_locked(project_id)
            return finished

    def _finish_scene_bridge_job_locked(
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
                result.error or "scene bridge execution failed",
                **kwargs,
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if (
            not result.output_revision
            or not is_safe_stable_id(result.output_revision)
            or not result.output_fingerprint
        ):
            finished = self.queue.mark_failed(
                current.job_id,
                "scene bridge returned no validated output identity",
                **kwargs,
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if current.status == "running":
            self.queue.mark_validating(current.job_id, **kwargs)
        try:
            request = self._load_scene_bridge_request(current)
            identity = request["runner_identity"]
            if not isinstance(identity, Mapping):
                raise ValueError("scene bridge runner identity is invalid")
            candidate_value = result.outputs.get("candidate_root")
            if not isinstance(candidate_value, str) or not current.attempts:
                raise ValueError("scene bridge candidate path is missing")
            attempt = Path(current.attempts[-1].directory).resolve(strict=True)
            candidate = Path(candidate_value).resolve(strict=True)
            candidate.relative_to(attempt)
            validated = validate_scene_bridge_candidate(candidate, identity)
            if (
                validated.status != "success"
                or validated.output_revision != result.output_revision
                or validated.output_fingerprint != result.output_fingerprint
                or validated.validation_proof != result.validation_proof
            ):
                raise ValueError("scene bridge candidate validation proof changed")
            published_root = (
                self.projects_root
                / current.project_id
                / "scene_bridges"
                / current.clip_id
                / result.output_revision
            )
            if published_root.exists():
                existing = validate_scene_bridge_candidate(published_root, identity)
                if (
                    existing.status != "success"
                    or existing.output_fingerprint != result.output_fingerprint
                ):
                    raise ValueError("scene bridge immutable revision collision")
            else:
                published_root.parent.mkdir(parents=True, exist_ok=True)
                temporary = published_root.with_name(
                    f".{published_root.name}.{uuid4().hex}.tmp"
                )
                try:
                    shutil.copytree(candidate, temporary)
                    for path in temporary.rglob("*"):
                        if path.is_file():
                            _fsync_regular_file(path)
                    os.replace(temporary, published_root)
                    _fsync_parent_directory(published_root.parent)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
                copied = validate_scene_bridge_candidate(published_root, identity)
                if (
                    copied.status != "success"
                    or copied.output_fingerprint != result.output_fingerprint
                ):
                    raise ValueError("published scene bridge revision is invalid")
            manifest_path = published_root / "scene_bridge_manifest.json"
            reference = StateReference(
                owner="jobs",
                key=f"job:{current.job_id}",
                operation_id=current.operation_id,
                value={
                    "reference_type": "scene_bridge",
                    "status": "awaiting_route_refinement",
                    "bridge_revision": result.output_revision,
                    "output_fingerprint": result.output_fingerprint,
                    "job_id": current.job_id,
                    "source_clip_id": str(identity["source_clip_id"]),
                    "target_clip_id": current.clip_id,
                    "direction": str(identity["direction"]),
                    "manifest_path": str(manifest_path),
                    "camera_track_path": str(
                        published_root / "camera_track_seed.json"
                    ),
                    "alignment_path": str(
                        published_root / "core_alignment/03_alignment/alignment.json"
                    ),
                    "camera_path": str(
                        published_root
                        / "core_alignment/03_alignment/sfm_camera_path.csv"
                    ),
                    "viewer_scene_path": str(
                        published_root
                        / "core_alignment/05_viewer_scene/sfm_viewer_scene.json"
                    ),
                    "source_workbench_output_revision": identity.get(
                        "source_workbench_output_revision"
                    ),
                    "source_workbench_output_fingerprint": identity.get(
                        "source_workbench_output_fingerprint"
                    ),
                },
            )
            clips = self.repositories.clips.load(current.project_id)
            if not any(clip.clip_id == current.clip_id for clip in clips.clips):
                raise ValueError("scene bridge target clip no longer exists")
            published_outputs = {
                name: str(
                    published_root
                    / Path(path).resolve(strict=True).relative_to(candidate)
                )
                for name, path in validated.outputs.items()
                if name != "candidate_root"
            }
            published_outputs["candidate_root"] = str(published_root)
            candidate_job = self.queue.prepare_success_candidate(
                current.job_id,
                output_revision=result.output_revision,
                output_fingerprint=result.output_fingerprint,
                output_validated=True,
                published_outputs=published_outputs,
                validation_proof=result.validation_proof,
                **kwargs,
            )
            jobs = self.repositories.jobs.load(current.project_id)

            def mutate_clips(
                value: ClipsManifest, _operation_id: str
            ) -> ClipsManifest:
                if not any(clip.clip_id == current.clip_id for clip in value.clips):
                    raise ValueError("scene bridge target clip no longer exists")
                return replace(
                    value,
                    updated_at=self.now(),
                    clips=tuple(
                        replace(
                            clip,
                            references=tuple(
                                item
                                for item in clip.references
                                if not (
                                    item.value.get("reference_type")
                                    == "scene_bridge"
                                    and item.value.get("target_clip_id")
                                    == current.clip_id
                                )
                            )
                            + (reference,),
                        )
                        if clip.clip_id == current.clip_id
                        else clip
                        for clip in value.clips
                    ),
                )

            def mutate_jobs(
                value: JobsManifest, operation_id: str
            ) -> JobsManifest:
                if not any(
                    item.get("job_id") == current.job_id for item in value.jobs
                ):
                    raise ValueError("scene bridge job is missing from jobs manifest")
                return replace(
                    value,
                    updated_at=self.now(),
                    jobs=tuple(
                        replace(
                            candidate_job,
                            operation_id=operation_id,
                            publication_operation_id=operation_id,
                        ).to_dict()
                        if item.get("job_id") == current.job_id
                        else dict(item)
                        for item in value.jobs
                    ),
                )

            publication = publish_manifests(
                (
                    ManifestMutation(
                        repository=self.repositories.clips,
                        project_id=current.project_id,
                        expected_revision=clips.revision,
                        mutate=mutate_clips,
                    ),
                    ManifestMutation(
                        repository=self.repositories.jobs,
                        project_id=current.project_id,
                        expected_revision=jobs.revision,
                        mutate=mutate_jobs,
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
            try:
                finished = self.queue.commit_prepared_candidate(
                    current.job_id, candidate=persisted, **kwargs
                )
            except Exception as exc:
                raise _SceneBridgePublicationPending(
                    current.project_id, current.job_id, exc
                ) from exc
            self.queue.acknowledge_publication(current.project_id)
            return finished
        except _SceneBridgePublicationPending:
            raise
        except Exception as exc:
            finished = self.queue.mark_failed(
                current.job_id,
                f"scene bridge publication failed: {exc}",
                **kwargs,
            )
        self._publish_queue_locked(current.project_id)
        return finished

    def _recover_scene_bridge_publication(
        self, pending: _SceneBridgePublicationPending
    ) -> QueueJob:
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
            if persisted is None or persisted.status != "success":
                raise RuntimeError(
                    "scene bridge publication could not be recovered"
                ) from pending.cause
            clips = self.repositories.clips.load(pending.project_id)
            target = next(
                (clip for clip in clips.clips if clip.clip_id == current.clip_id),
                None,
            )
            reference = (
                None
                if target is None
                else next(
                    (
                        item
                        for item in reversed(target.references)
                        if item.owner == "jobs"
                        and item.key == f"job:{current.job_id}"
                        and item.value.get("reference_type") == "scene_bridge"
                        and item.value.get("job_id") == current.job_id
                    ),
                    None,
                )
            )
            if (
                target is None
                or reference is None
                or not _validate_scene_bridge_reference(
                    self.projects_root,
                    pending.project_id,
                    target,
                    reference,
                )
                or persisted.output_revision
                != reference.value.get("bridge_revision")
                or persisted.output_fingerprint
                != reference.value.get("output_fingerprint")
            ):
                raise RuntimeError(
                    "scene bridge durable publication is inconsistent"
                ) from pending.cause
            attempt = current.attempts[-1]
            committed = self.queue.commit_prepared_candidate(
                pending.job_id,
                candidate=persisted,
                attempt_number=attempt.number,
                claim_token=attempt.worker_claim_token,
            )
            self.queue.acknowledge_publication(pending.project_id)
            return committed

    def _finish_cad_replacement_job_locked(
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
            self._record_cad_replacement_state_locked(
                current, "stale_input", progress=None, error=None
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if result.status != "success":
            error = result.error or "CAD replacement adapter execution failed"
            finished = self.queue.mark_failed(current.job_id, error, **kwargs)
            self._record_cad_replacement_state_locked(
                current, "failed", progress=None, error=error
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if result.output_revision is None or result.output_fingerprint is None:
            error = "CAD replacement adapter returned no validated output identity"
            finished = self.queue.mark_failed(current.job_id, error, **kwargs)
            self._record_cad_replacement_state_locked(
                current, "failed", progress=None, error=error
            )
            self._publish_queue_locked(current.project_id)
            return finished
        if current.status == "running":
            self.queue.mark_validating(current.job_id, **kwargs)
        cad_source = validate_result_path(current, result)
        artifact = self.analysis_publisher.publish_cad(
            cad_source=cad_source,
            cad_fingerprint=result.output_fingerprint,
        )
        project = self.repositories.project.load(current.project_id)
        state = project.source_assets.get("_cad_replacement")
        if (
            not isinstance(state, Mapping)
            or state.get("job_id") != current.job_id
            or not isinstance(state.get("candidate"), Mapping)
        ):
            raise ValueError("CAD replacement candidate changed before publication")
        candidate_descriptor = dict(state["candidate"])
        active_descriptor = project.source_assets.get("cad")
        if not isinstance(active_descriptor, Mapping):
            raise ValueError("project active CAD descriptor is unavailable")
        active_descriptor = _complete_active_cad_descriptor(
            dict(active_descriptor), self.repositories.clips.load(current.project_id)
        )
        new_descriptor = {
            **candidate_descriptor,
            "revision": str(result.output_revision),
            "dataset_id": artifact.dataset_id,
            "dataset_path": str(artifact.dataset_path),
            "activated_at": self.now(),
        }
        versions = list(project.source_assets.get("_cad_versions", ()))
        if not versions:
            versions.append(
                {
                    **active_descriptor,
                    "revision": str(
                        active_descriptor.get("revision")
                        or _cad_descriptor_revision(active_descriptor)
                    ),
                    "status": "superseded",
                }
            )
        else:
            versions = [
                {
                    **dict(item),
                    "status": "superseded"
                    if isinstance(item, Mapping) and item.get("status") == "active"
                    else (item.get("status") if isinstance(item, Mapping) else None),
                }
                for item in versions
                if isinstance(item, Mapping)
            ]
        versions.append({**new_descriptor, "status": "active"})
        candidate_job = self.queue.prepare_success_candidate(
            current.job_id,
            output_revision=str(result.output_revision),
            output_fingerprint=result.output_fingerprint,
            output_validated=True,
            published_outputs={"cad_dataset": str(artifact.dataset_path)},
            **kwargs,
        )
        jobs_manifest = self.repositories.jobs.load(current.project_id)
        render_manifest = self.repositories.render.load(current.project_id)

        def mutate_project(
            value: ProjectManifest, operation_id: str
        ) -> ProjectManifest:
            current_state = value.source_assets.get("_cad_replacement")
            if (
                not isinstance(current_state, Mapping)
                or current_state.get("job_id") != current.job_id
            ):
                raise ValueError("CAD replacement state changed during publication")
            assets = dict(value.source_assets)
            assets["cad"] = new_descriptor
            assets["_cad_versions"] = versions
            assets["_cad_replacement"] = {
                **dict(current_state),
                "status": "success",
                "active_revision": str(result.output_revision),
                "completed_at": self.now(),
                "operation_id": operation_id,
                "progress": {
                    "stage": "success",
                    "fraction": 1.0,
                    "message": "CAD 图纸替换完成",
                },
                "error": None,
            }
            return replace(
                value,
                source_assets=assets,
                updated_at=self.now(),
                operation_id=operation_id,
            )

        def mutate_jobs(value: JobsManifest, operation_id: str) -> JobsManifest:
            return replace(
                value,
                updated_at=self.now(),
                jobs=tuple(
                    replace(
                        candidate_job,
                        operation_id=operation_id,
                        publication_operation_id=operation_id,
                    ).to_dict()
                    if item.get("job_id") == current.job_id
                    else dict(item)
                    for item in value.jobs
                ),
            )

        def stale(item: Mapping[str, object]) -> dict[str, object]:
            return {
                **dict(item),
                "status": "stale_input",
                "stale_reason": "cad_revision_changed",
            }

        def mutate_render(
            value: RenderManifest, _operation_id: str
        ) -> RenderManifest:
            return replace(
                value,
                updated_at=self.now(),
                clip_renders=tuple(stale(item) for item in value.clip_renders),
                merge_plans=tuple(stale(item) for item in value.merge_plans),
                published_outputs=tuple(
                    stale(item) for item in value.published_outputs
                ),
            )

        publication = publish_manifests(
            (
                ManifestMutation(
                    repository=self.repositories.project,
                    project_id=current.project_id,
                    expected_revision=project.revision,
                    mutate=mutate_project,
                ),
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
            item for item in publication.manifests if isinstance(item, JobsManifest)
        )
        persisted = QueueJob.from_dict(
            next(
                item
                for item in persisted_jobs.jobs
                if item.get("job_id") == current.job_id
            )
        )
        committed = self.queue.commit_prepared_candidate(
            current.job_id, candidate=persisted, **kwargs
        )
        self.queue.acknowledge_publication(current.project_id)
        return committed

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
        validated = self._validate_render_files(
            job,
            output_revision=result.output_revision,
            video_path=video,
            frame_map_path=frame_map_path,
        )
        publication_identity = {
            "schema_version": 1,
            "project_id": job.project_id,
            "clip_id": job.clip_id,
            "job_id": job.job_id,
            "input_revision": job.input_revision,
            "input_fingerprint": job.input_fingerprint,
            "adapter_name": job.adapter_name,
            "adapter_version": job.adapter_version,
            "adapter_output_revision": result.output_revision,
            "validated_output_fingerprint": validated.output_fingerprint,
        }
        fingerprint = sha256(
            json.dumps(
                publication_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return replace(validated, output_revision=f"render-{fingerprint[:16]}")

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
        stored_jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(job.project_id).jobs
        )
        _, source_map_path = _render_physical_inputs(clip, stored_jobs)
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
                "rendered video differs from the media specification for this project: "
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

    def _record_cad_replacement_state_locked(
        self,
        job: QueueJob,
        status: str,
        *,
        progress: Mapping[str, object] | None,
        error: str | None,
    ) -> None:
        current = self.repositories.project.load(job.project_id)
        state = current.source_assets.get("_cad_replacement")
        if not isinstance(state, Mapping) or state.get("job_id") != job.job_id:
            return
        updated_state = dict(state)
        updated_state.update({"status": status, "error": error})
        if progress is not None:
            updated_state["progress"] = dict(progress)
        assets = dict(current.source_assets)
        assets["_cad_replacement"] = updated_state
        self.repositories.project.update(
            job.project_id,
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value, source_assets=assets, updated_at=self.now()
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
            elif current.job_type == "cad_replacement":
                self._record_cad_replacement_state_locked(
                    current,
                    cancelled.status,
                    progress=None,
                    error=cancelled.error,
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
                elif job.job_type == "cad_replacement":
                    self._record_cad_replacement_state_locked(
                        job, "superseded", progress=None, error=None
                    )
                self._publish_queue_locked(project_id)
                raise RuntimeError("job input changed before execution")
            return self._build_job_execution_plan_locked(job)

    def _build_job_execution_plan_locked(
        self, job: QueueJob
    ) -> JobExecutionPlan:
        project_id = job.project_id
        if job.job_type == "cad_georeference_candidates":
            return self._prepare_cad_georeference_candidate_plan(job)
        if job.job_type == "clip_export":
            return self._prepare_clip_export(job)
        if job.job_type == "sfm_solve_export":
            return self._prepare_solve_export(job)
        if job.job_type in {"cad_analysis", "video_analysis", "cad_replacement"}:
            return self._prepare_analysis(job)
        if job.job_type == "clip_render":
            return self._prepare_clip_render(job)
        if job.job_type == "project_merge":
            return self._prepare_project_merge(job)
        if job.job_type == "scene_bridge":
            return self._prepare_scene_bridge(job)
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
        core_frame_map_path = frame_map_path
        for dependency_id in job.depends_on_job_ids:
            dependency = self.queue.get(dependency_id)
            if dependency.status != "success" or not dependency.output_validated:
                raise RuntimeError("trajectory media dependency is not validated")
            if dependency.job_type == "clip_export":
                video_value = dependency.published_outputs.get(
                    f"video:{clip.clip_id}"
                )
                map_value = dependency.published_outputs.get(
                    f"frame_map:{clip.clip_id}"
                )
                video_path = None if video_value is None else Path(video_value)
                frame_map_path = None if map_value is None else Path(map_value)
                core_frame_map_path = frame_map_path
            elif dependency.job_type == "sfm_solve_export":
                video_value = dependency.published_outputs.get(
                    f"solve_video:{clip.clip_id}"
                )
                map_value = dependency.published_outputs.get(
                    f"solve_frame_map:{clip.clip_id}"
                )
                video_path = None if video_value is None else Path(video_value)
                frame_map_path = None if map_value is None else Path(map_value)
            else:
                raise RuntimeError("unsupported trajectory media dependency")
        if video_path is None:
            raise FileNotFoundError("physical clip MP4 is unavailable")
        time_base = _fraction_time_base(clip)
        inputs = AdapterInputs(
            project_id=project_id,
            clip_id=clip.clip_id,
            video_path=video_path,
            srt_path=_clip_asset_path(clip, project.source_assets, "srt"),
            attempt_directory=Path(job.attempts[-1].directory),
            parameters=(
                _srt_full_pose_adapter_parameters(
                    self.projects_root, project, clip
                )
                if adapter.name == "srt_full_pose"
                else (
                    _fixed_track_visual_pose_adapter_parameters(
                        self.projects_root, project, clip
                    )
                    if adapter.name == "srt_fixed_track_visual_pose"
                    else dict(clip.manual_definition)
                )
            ),
            source_start_pts=int(clip.analysis["source_start_pts"]),
            source_end_pts_exclusive=int(clip.analysis["source_end_pts_exclusive"]),
            source_time_base=time_base,
            frame_map_path=frame_map_path,
            core_frame_map_path=(
                core_frame_map_path
                if adapter.name == "sfm_only"
                else None
            ),
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
        authoritative_frames = _load_authoritative_source_frames(clip, frame_map)
        source_time_base = _fraction_time_base(clip)
        annotation_bundle = self._write_annotation_render_bundle(
            job.project_id,
            clip,
            frames=authoritative_frames,
            time_base=source_time_base,
            media_spec=media_spec,
            attempt=attempt,
        )
        render_parameters = {
            **dict(clip.manual_definition),
            **_workbench_render_parameters(
                self.storage_root,
                job.project_id,
                clip,
                self.repositories.project.load(job.project_id).source_assets,
            ),
            "trajectory_path": dependency.published_outputs["trajectory"],
        }
        if annotation_bundle is not None:
            render_parameters["annotation_render_bundle_path"] = str(
                annotation_bundle
            )
        inputs = RenderInputs(
            project_id=job.project_id,
            clip_id=clip.clip_id,
            workflow=str(clip.resolved_workflow),
            physical_video_path=physical_video.resolve(strict=True),
            authoritative_frame_map_path=frame_map.resolve(strict=True),
            authoritative_source_frames=authoritative_frames,
            source_time_base=source_time_base,
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
            parameters=render_parameters,
        )
        plan = adapter.prepare(inputs)
        return JobExecutionPlan(commands=plan.commands, validate=plan.validate)

    def _prepare_project_merge(self, job: QueueJob) -> JobExecutionPlan:
        if (
            job.status != "running"
            or job.resource_class != "media_io"
            or job.exclusive_key != f"merge:{job.project_id}"
            or not job.attempts
        ):
            raise RuntimeError("project merge scheduling contract is invalid")
        request = self._project_concat_request(job.project_id)
        plan = build_concat_plan(request)
        identity_payload = {
            "plan": plan.to_dict(),
            "render_job_ids": list(job.depends_on_job_ids),
            "adapter_name": self.concat_adapter.name,
            "adapter_version": self.concat_adapter.version,
        }
        if _fingerprint(identity_payload) != job.input_fingerprint:
            raise RuntimeError("project merge inputs changed before execution")
        attempt = Path(job.attempts[-1].directory).resolve(strict=True)
        _, media_spec = _project_media_binding(
            self.repositories.project.load(job.project_id)
        ) or (None, None)
        if media_spec is None:
            raise RuntimeError("project media specification is unavailable")
        inputs = ConcatMediaInputs(
            plan=plan,
            source_frame_index=request.source_frame_index,
            source_video_path=Path(request.original_video_path).resolve(strict=True),
            attempt_directory=attempt,
            project_media_spec=media_spec,
        )
        execution = self.concat_adapter.prepare(inputs)
        execution_path = attempt / "concat_execution.json"
        spec_path = attempt / "project_media_spec.json"
        execution_path.write_text(
            json.dumps(execution.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        spec_path.write_text(
            json.dumps(media_spec.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        command = (
            sys.executable,
            "-m",
            "cadscene.cli.concat_media",
            "--execution-plan-json",
            str(execution_path),
            "--project-media-spec-json",
            str(spec_path),
            "--attempt-directory",
            str(attempt),
        )
        return JobExecutionPlan(commands=(command,), validate=execution.validate)

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
            elif current.job_type == "cad_replacement":
                self._record_cad_replacement_state_locked(
                    current,
                    str(progress.stage),
                    progress={
                        "stage": str(progress.stage),
                        "fraction": progress.fraction,
                        "message": progress.message,
                    },
                    error=None,
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
            elif current.job_type == "cad_replacement":
                self._record_cad_replacement_state_locked(
                    current, "failed", progress=None, error=error
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
            elif current.job_type == "cad_replacement":
                self._record_cad_replacement_state_locked(
                    current,
                    "queued",
                    progress={
                        "stage": "queued",
                        "fraction": 0.0,
                        "message": "等待重试 CAD 图纸替换",
                    },
                    error=None,
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
            manifest = self._recover_restored_scene_bridge_jobs_locked(
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
            self._sync_cad_replacement_state_from_queue_locked(project_id)
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

    def _recover_restored_scene_bridge_jobs_locked(
        self, project_id: str, jobs_manifest: JobsManifest
    ) -> JobsManifest:
        clips = self.repositories.clips.load(project_id)
        clips_by_id = {clip.clip_id: clip for clip in clips.clips}
        recovered: dict[str, QueueJob] = {}
        repair_operation_id: str | None = None
        for payload in jobs_manifest.jobs:
            job = QueueJob.from_dict(payload)
            if (
                job.job_type != "scene_bridge"
                or job.status not in {"stale_input", "superseded"}
            ):
                continue
            clip = clips_by_id.get(job.clip_id)
            if clip is None:
                continue
            reference = next(
                (
                    item
                    for item in reversed(clip.references)
                    if item.owner == "jobs"
                    and item.key == f"job:{job.job_id}"
                    and item.value.get("reference_type") == "scene_bridge"
                    and item.value.get("status") == "awaiting_route_refinement"
                ),
                None,
            )
            if reference is None or not _validate_scene_bridge_reference(
                self.projects_root, project_id, clip, reference
            ):
                continue
            try:
                if self._current_input_fingerprint(job) != job.input_fingerprint:
                    continue
                request = self._load_scene_bridge_request(job)
                revision = str(reference.value["bridge_revision"])
                root = self.projects_root / project_id / "scene_bridges" / clip.clip_id / revision
                manifest = json.loads(
                    (root / "scene_bridge_manifest.json").read_text(encoding="utf-8-sig")
                )
                identity = manifest.get("identity")
                if not isinstance(identity, Mapping):
                    continue
                validated = validate_scene_bridge_candidate(root, identity)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if (
                validated.status != "success"
                or validated.output_revision != revision
                or validated.output_fingerprint
                != reference.value.get("output_fingerprint")
                or not isinstance(validated.validation_proof, Mapping)
            ):
                continue
            if repair_operation_id is None:
                repair_operation_id = self._identity()
            recovered[job.job_id] = replace(
                job,
                status="success",
                stage="success",
                operation_id=repair_operation_id,
                submission_operation_id=(
                    job.submission_operation_id or str(request["operation_id"])
                ),
                publication_operation_id=repair_operation_id,
                output_revision=validated.output_revision,
                output_fingerprint=validated.output_fingerprint,
                output_validated=True,
                validated_input_fingerprint=job.input_fingerprint,
                published_outputs=dict(validated.outputs),
                validation_proof=dict(validated.validation_proof),
                progress={
                    "stage": "complete",
                    "message": "completed",
                    "fraction": 1.0,
                },
                error=None,
                cleanup_reason=None,
                target_terminal_status=None,
            )
        if not recovered:
            return jobs_manifest
        assert repair_operation_id is not None
        return self.repositories.jobs.update(
            project_id,
            expected_revision=jobs_manifest.revision,
            mutate=lambda value: replace(
                value,
                jobs=tuple(
                    recovered.get(str(item.get("job_id")), QueueJob.from_dict(item)).to_dict()
                    for item in value.jobs
                ),
            ),
        )

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
            identity_matches = (
                job.input_fingerprint == legacy_fingerprint
                and job.idempotency_key == legacy_idempotency
            )
            if not identity_matches:
                snapshot = self._legacy_analysis_snapshot_assets(
                    project.source_assets,
                    video_job=structured[1],
                    request_key=request_key,
                )
                if snapshot is not None:
                    snapshot_contract = _analysis_job_contract(
                        project_id=project_id,
                        phase=phase,
                        request_key=request_key,
                        project_assets=snapshot,
                    )
                    legacy_payload = _legacy_analysis_identity_payload(
                        phase=phase,
                        request_key=request_key,
                        project_assets=snapshot,
                    )
                    legacy_fingerprint = _fingerprint(legacy_payload)
                    legacy_idempotency = _fingerprint(
                        {**legacy_payload, "purpose": "idempotency"}
                    )
                    identity_matches = (
                        job.input_fingerprint
                        == snapshot_contract["input_fingerprint"]
                        and job.idempotency_key
                        == snapshot_contract["idempotency_key"]
                    ) or (
                        job.input_fingerprint == legacy_fingerprint
                        and job.idempotency_key == legacy_idempotency
                    )
            if not identity_matches:
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

    @staticmethod
    def _legacy_analysis_snapshot_assets(
        project_assets: Mapping[str, object],
        *,
        video_job: QueueJob,
        request_key: str,
    ) -> Mapping[str, object] | None:
        revisions = project_assets.get("_analysis_revisions")
        if not isinstance(revisions, Mapping):
            return None
        descriptor = revisions.get(f"analysis-{video_job.job_id}")
        if not isinstance(descriptor, Mapping):
            return None
        snapshot = descriptor.get("input_snapshot")
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("request_key") != request_key
        ):
            return None
        for name in ("video", "cad"):
            asset = snapshot.get(name)
            if not isinstance(asset, Mapping) or any(
                not isinstance(asset.get(field), str) or not asset.get(field)
                for field in ("path", "sha256")
            ):
                return None
        srt = snapshot.get("srt")
        if srt is not None and (
            not isinstance(srt, Mapping)
            or any(
                not isinstance(srt.get(field), str) or not srt.get(field)
                for field in ("path", "sha256")
            )
        ):
            return None
        return snapshot

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

    def _sync_cad_replacement_state_from_queue_locked(
        self, project_id: str
    ) -> None:
        project = self.repositories.project.load(project_id)
        state = project.source_assets.get("_cad_replacement")
        if not isinstance(state, Mapping) or not isinstance(state.get("job_id"), str):
            return
        try:
            job = self.queue.get(str(state["job_id"]))
        except KeyError:
            return
        if job.project_id != project_id or job.job_type != "cad_replacement":
            return
        progress = None if job.progress is None else dict(job.progress)
        if (
            state.get("status") == job.status
            and state.get("error") == job.error
            and (progress is None or state.get("progress") == progress)
        ):
            return
        updated = dict(state)
        updated.update({"status": job.status, "error": job.error})
        if progress is not None:
            updated["progress"] = progress
        assets = dict(project.source_assets)
        assets["_cad_replacement"] = updated
        self.repositories.project.update(
            project_id,
            expected_revision=project.revision,
            mutate=lambda value: replace(
                value, source_assets=assets, updated_at=self.now()
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
                        self._sync_cad_replacement_state_from_queue_locked(
                            project_id
                        )
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

    def _new_solve_export_job(
        self,
        project_id: str,
        clip: ClipDefinition,
        *,
        solve_interval: SolveInterval,
        project_assets: Mapping[str, object],
        project_revision: int,
        clips_revision: int,
    ) -> QueueJob:
        identity_payload = _solve_export_identity_payload(
            clip=clip,
            solve_interval=solve_interval,
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
            job_type="sfm_solve_export",
            resource_class="media_io",
            status="queued",
            stage="queued",
            priority=0,
            depends_on_job_ids=(),
            exclusive_key=f"sfm_solve_export:{project_id}:{clip.clip_id}",
            idempotency_key=_fingerprint(
                {**identity_payload, "purpose": "idempotency"}
            ),
            input_revision=clip.analysis_revision,
            input_fingerprint=input_fingerprint,
            adapter_name="sfm_solve_export",
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
        annotation_identity = self._annotation_render_identity(project_id, clip)
        active_cad_identity = _active_cad_render_identity(
            self.repositories.project.load(project_id).source_assets, clip
        )
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
            annotation_identity=annotation_identity,
            active_cad_identity=active_cad_identity,
        )
        input_fingerprint = _fingerprint(identity_payload)
        revision_fingerprint = _fingerprint(
            {
                "analysis_revision": clip.analysis_revision,
                "workbench_output_revision": str(
                    workbench.value["workbench_output_revision"]
                ),
                "media_spec_revision": media_spec_revision,
                "annotation_dependencies": annotation_identity,
                "active_cad": active_cad_identity,
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

    def _annotation_render_identity(
        self, project_id: str, clip: ClipDefinition
    ) -> Mapping[str, object]:
        manifest = self.repositories.annotations.load(project_id)
        annotations = tuple(
            sorted(
                (
                    item
                    for item in manifest.annotations
                    if item.clip_id == clip.clip_id
                ),
                key=lambda item: item.annotation_id,
            )
        )
        tracking_dependencies = []
        has_cad_anchor = False
        for annotation in annotations:
            if annotation.anchor_type == "cad_anchor":
                has_cad_anchor = True
                continue
            revision_id = annotation.active_tracking_revision
            if revision_id is None:
                # 未完成跟踪的视频标牌属于可保存草稿；渲染时按位置为空隐藏，
                # 不能阻断同片段内已经可用的 CAD 或视频标牌。
                continue
            revision = self.tracking_revision_repository.load(
                project_id,
                annotation.clip_id,
                annotation.annotation_id,
                revision_id,
            )
            tracking_dependencies.append(
                {
                    "annotation_id": annotation.annotation_id,
                    "tracking_revision": revision.tracking_revision,
                    "source_video_fingerprint": revision.source_video_fingerprint,
                    "clip_revision": revision.clip_revision,
                    "tracker_name": revision.tracker_name,
                    "tracker_version": revision.tracker_version,
                    "initialization": revision.initialization.to_dict(),
                    "corrections": [
                        item.to_dict() for item in revision.corrections
                    ],
                }
            )
        cad_revision = None
        cad_coordinate_transform = None
        if has_cad_anchor:
            cad = self.repositories.project.load(project_id).source_assets.get("cad")
            cad_revision = (
                {
                    key: cad.get(key)
                    for key in (
                        "sha256",
                        "dataset_id",
                        "dataset_path",
                        "artifact_id",
                    )
                    if cad.get(key) is not None
                }
                if isinstance(cad, Mapping)
                else cad
            )
            parameters = _workbench_render_parameters(
                self.storage_root,
                project_id,
                clip,
                self.repositories.project.load(project_id).source_assets,
            )
            cad_coordinate_transform = {
                "cad_scale": float(parameters["cad_scale"]),
                "origin_xy": [float(value) for value in parameters["origin_xy"]],
            }
        return {
            "annotations": [item.to_dict() for item in annotations],
            "tracking_dependencies": tracking_dependencies,
            "cad_revision": cad_revision,
            **(
                {"cad_coordinate_transform": cad_coordinate_transform}
                if cad_coordinate_transform is not None
                else {}
            ),
        }

    def _write_annotation_render_bundle(
        self,
        project_id: str,
        clip: ClipDefinition,
        *,
        frames: Sequence[DecodedFrameTimestamp],
        time_base: Fraction,
        media_spec: ProjectMediaSpec,
        attempt: Path,
    ) -> Path | None:
        identity = self._annotation_render_identity(project_id, clip)
        raw_annotations = identity["annotations"]
        if not raw_annotations:
            return None
        tracking_revisions: dict[str, object] = {}
        for annotation in self.repositories.annotations.load(project_id).annotations:
            if annotation.clip_id != clip.clip_id:
                continue
            revision_id = annotation.active_tracking_revision
            if revision_id is None:
                continue
            revision = self.tracking_revision_repository.load(
                project_id,
                clip.clip_id,
                annotation.annotation_id,
                revision_id,
            )
            tracking_revisions[revision_id] = revision.to_dict()
        bundle = {
            "schema_version": 1,
            "project_id": project_id,
            "clip_id": clip.clip_id,
            "video_width": media_spec.width,
            "video_height": media_spec.height,
            "source_time_base": {
                "numerator": time_base.numerator,
                "denominator": time_base.denominator,
            },
            "source_frames": [
                {
                    "source_decoded_frame_ordinal": frame.ordinal,
                    "source_pts": frame.pts,
                    "duration_pts": frame.duration_pts,
                }
                for frame in frames
            ],
            "annotations": raw_annotations,
            "tracking_revisions": tracking_revisions,
            "dependencies": identity,
            **(
                {"cad_coordinate_transform": identity["cad_coordinate_transform"]}
                if "cad_coordinate_transform" in identity
                else {}
            ),
        }
        path = attempt / "annotation_render_bundle.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(bundle, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _current_input_fingerprint(self, job: QueueJob) -> str | None:
        project = self.repositories.project.load(job.project_id)
        if job.job_type == "cad_georeference_candidates":
            try:
                request = self._load_cad_georeference_candidate_request(job)
                request_assets = _complete_cad_georeference_assets(
                    project.source_assets,
                    self.repositories.clips.load(job.project_id),
                )
                current = _cad_georeference_candidate_request(
                    job.project_id,
                    request_assets,
                    central_meridian_deg=(
                        None
                        if request.get("central_meridian_deg") is None
                        else float(request["central_meridian_deg"])
                    ),
                    limit=int(request["limit"]),
                )
                return _fingerprint(current)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                return None
        if job.job_type == "cad_replacement":
            state = project.source_assets.get("_cad_replacement")
            if (
                not isinstance(state, Mapping)
                or state.get("job_id") != job.job_id
                or state.get("candidate_revision") != job.input_revision
                or not isinstance(state.get("candidate"), Mapping)
            ):
                return None
            return _fingerprint(
                _cad_replacement_identity_payload(
                    project_id=job.project_id,
                    candidate_revision=job.input_revision,
                    candidate=state["candidate"],
                )
            )
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
        if job.job_type == "project_merge":
            try:
                plan = build_concat_plan(self._project_concat_request(job.project_id))
                return _fingerprint(
                    {
                        "plan": plan.to_dict(),
                        "render_job_ids": list(job.depends_on_job_ids),
                        "adapter_name": self.concat_adapter.name,
                        "adapter_version": self.concat_adapter.version,
                    }
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
        if job.job_type == "scene_bridge":
            try:
                request = self._load_scene_bridge_request(job)
                clips_manifest = self.repositories.clips.load(job.project_id)
                source = next(
                    (
                        clip
                        for clip in clips_manifest.clips
                        if clip.clip_id == request["runner_identity"]["source_clip_id"]
                    ),
                    None,
                )
                target = next(
                    (
                        clip
                        for clip in clips_manifest.clips
                        if clip.clip_id == request["runner_identity"]["target_clip_id"]
                    ),
                    None,
                )
                if source is None or target is None or target.clip_id != job.clip_id:
                    return None
                source_workbench = _saved_workbench_reference(source)
                if source_workbench is None or source_workbench.value.get("status") != "saved":
                    return None
                stored_jobs = tuple(
                    QueueJob.from_dict(item)
                    for item in self.repositories.jobs.load(job.project_id).jobs
                )
                jobs_by_id = {item.job_id: item for item in stored_jobs}
                source_job = jobs_by_id.get(str(request["source_trajectory_job_id"]))
                target_job = jobs_by_id.get(str(request["target_trajectory_job_id"]))
                if (
                    source_job is None
                    or target_job is None
                    or self._current_trajectory_for_render(source, (source_job,))
                    != source_job
                    or target_job.job_type != "trajectory"
                    or target_job.clip_id != target.clip_id
                    or self._current_input_fingerprint(target_job)
                    != target_job.input_fingerprint
                ):
                    return None
                current_identity = _scene_bridge_identity_payload(
                    project=project,
                    source=source,
                    target=target,
                    source_workbench=source_workbench,
                    source_trajectory=source_job,
                    target_trajectory=target_job,
                    direction=str(request["runner_identity"]["direction"]),
                    source_core_frame_map=_core_frame_map_identity(
                        source, stored_jobs
                    ),
                    target_core_frame_map=_core_frame_map_identity(
                        target, stored_jobs
                    ),
                )
                return _fingerprint(current_identity)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                return None
        clips_manifest = self.repositories.clips.load(job.project_id)
        if job.job_type in {"clip_export", "sfm_solve_export"}:
            clip = next(
                (item for item in clips_manifest.clips if item.clip_id == job.clip_id),
                None,
            )
            if clip is None:
                return None
            if job.job_type == "sfm_solve_export":
                try:
                    stored_jobs = tuple(
                        QueueJob.from_dict(item)
                        for item in self.repositories.jobs.load(job.project_id).jobs
                    )
                    solve_interval = derive_scene_solve_interval(
                        clips=clips_manifest.clips,
                        clip_id=clip.clip_id,
                        source_frame_index=_project_source_frame_index(
                            clips_manifest.clips, stored_jobs
                        ),
                    )
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    return None
                return _fingerprint(
                    _solve_export_identity_payload(
                        clip=clip,
                        solve_interval=solve_interval,
                        project_assets=project.source_assets,
                        project_revision=project.revision,
                        clips_revision=clips_manifest.revision,
                    )
                )
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
                        annotation_identity=self._annotation_render_identity(
                            job.project_id, clip
                        ),
                        active_cad_identity=_active_cad_render_identity(
                            project.source_assets, clip
                        ),
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

    def _load_scene_bridge_request(self, job: QueueJob) -> Mapping[str, object]:
        requests_root = (
            self.projects_root
            / job.project_id
            / "scene_bridge_requests"
        )
        preferred_ids = tuple(
            dict.fromkeys(
                value
                for value in (job.submission_operation_id, job.operation_id)
                if isinstance(value, str) and value
            )
        )
        candidates = [requests_root / f"{value}.json" for value in preferred_ids]
        if requests_root.is_dir():
            candidates.extend(
                path
                for path in sorted(requests_root.glob("*.json"))
                if path not in candidates
            )
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if _scene_bridge_request_matches_job(payload, job, path):
                return payload
        raise FileNotFoundError("scene bridge request identity is unavailable")

    def _prepare_scene_bridge(self, job: QueueJob) -> JobExecutionPlan:
        request = self._load_scene_bridge_request(job)
        runner_identity = request["runner_identity"]
        if not isinstance(runner_identity, Mapping):
            raise ValueError("scene bridge runner identity is invalid")
        project = self.repositories.project.load(job.project_id)
        clips_manifest = self.repositories.clips.load(job.project_id)
        source = next(
            (
                clip
                for clip in clips_manifest.clips
                if clip.clip_id == runner_identity.get("source_clip_id")
            ),
            None,
        )
        target = next(
            (
                clip
                for clip in clips_manifest.clips
                if clip.clip_id == runner_identity.get("target_clip_id")
            ),
            None,
        )
        if source is None or target is None or target.clip_id != job.clip_id:
            raise ValueError("scene bridge clips are unavailable")
        source_workbench = _saved_workbench_reference(source)
        if source_workbench is None or source_workbench.value.get("status") != "saved":
            raise ValueError("source saved route is unavailable")
        source_trajectory = self.queue.get(str(request["source_trajectory_job_id"]))
        target_trajectory = self.queue.get(str(request["target_trajectory_job_id"]))
        if (
            self._current_trajectory_for_render(source, (source_trajectory,))
            != source_trajectory
            or self._current_trajectory_for_render(target, (target_trajectory,))
            != target_trajectory
        ):
            raise ValueError("scene bridge trajectory dependency is not current")
        jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(job.project_id).jobs
        )
        _source_core_video, source_core_map = _render_physical_inputs(source, jobs)
        target_core_video, target_core_map = _render_physical_inputs(target, jobs)
        source_track = _workbench_artifact_path(
            self.projects_root, job.project_id, source_workbench
        )
        render_parameters = _workbench_render_parameters(
            self.storage_root,
            job.project_id,
            target,
            project.source_assets,
        )
        cad_dataset = render_parameters.get("cad_dataset_path")
        if not isinstance(cad_dataset, str) or not cad_dataset:
            raise FileNotFoundError("scene bridge CAD dataset is missing")
        cad_dir = Path(cad_dataset)
        if not (cad_dir / "design.json").is_file():
            raise FileNotFoundError("scene bridge CAD design is missing")
        source_trajectory_path = Path(source_trajectory.published_outputs["trajectory"])
        target_trajectory_path = Path(target_trajectory.published_outputs["trajectory"])
        source_solve_video = Path(source_trajectory.published_outputs["solve_video"])
        source_solve_map = Path(
            source_trajectory.published_outputs["solve_frame_map"]
        )
        source_solve_trajectory = Path(
            source_trajectory.published_outputs["solve_trajectory"]
        )
        target_solve_video = Path(target_trajectory.published_outputs["solve_video"])
        target_solve_map = Path(
            target_trajectory.published_outputs["solve_frame_map"]
        )
        target_solve_trajectory = Path(
            target_trajectory.published_outputs["solve_trajectory"]
        )
        source_sparse = source_trajectory_path.with_name("sparse_points.ply")
        target_sparse = target_trajectory_path.with_name("sparse_points.ply")
        if not source_sparse.is_file() or not target_sparse.is_file():
            raise FileNotFoundError("scene bridge SfM sparse point cloud is missing")
        origin = render_parameters.get("origin_xy", [0.0, 0.0])
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            raise ValueError("scene bridge origin_xy is invalid")
        inputs = SceneBridgeInputs(
            identity=dict(runner_identity),
            application_root=application_root(),
            attempt_directory=Path(job.attempts[-1].directory),
            source_core_frame_map_path=source_core_map,
            source_manual_track_path=source_track,
            source_solve_video_path=source_solve_video,
            source_solve_frame_map_path=source_solve_map,
            source_solve_trajectory_path=source_solve_trajectory,
            source_sparse_ply_path=source_sparse,
            target_solve_video_path=target_solve_video,
            target_solve_frame_map_path=target_solve_map,
            target_solve_trajectory_path=target_solve_trajectory,
            target_solve_sparse_ply_path=target_sparse,
            target_core_video_path=target_core_video,
            target_core_frame_map_path=target_core_map,
            target_core_trajectory_path=target_trajectory_path,
            target_core_sparse_ply_path=target_sparse,
            cad_dir=cad_dir,
            cad_scale=float(render_parameters.get("cad_scale", 0.06)),
            origin_xy=(float(origin[0]), float(origin[1])),
            overlap_seconds=Fraction(4, 1),
        )
        inputs_path = Path(job.attempts[-1].directory) / "scene_bridge_inputs.json"
        inputs.write_json(inputs_path)
        return JobExecutionPlan(
            commands=(
                (
                    sys.executable,
                    "-m",
                    "cadscene.cli.run_scene_bridge",
                    "--inputs",
                    str(inputs_path),
                ),
            ),
            validate=lambda: validate_scene_bridge_candidate(
                Path(job.attempts[-1].directory) / "candidate",
                runner_identity,
            ),
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
            "--preset",
            "veryfast",
            "--allow-subset",
        )
        return JobExecutionPlan(
            commands=(command,),
            validate=lambda: _validate_clip_export_outputs((clip,), output_dir),
        )

    def _prepare_solve_export(self, job: QueueJob) -> JobExecutionPlan:
        project = self.repositories.project.load(job.project_id)
        clips_manifest = self.repositories.clips.load(job.project_id)
        clip = next(
            (item for item in clips_manifest.clips if item.clip_id == job.clip_id),
            None,
        )
        if clip is None:
            raise KeyError(f"clip no longer exists: {job.clip_id}")
        stored_jobs = tuple(
            QueueJob.from_dict(item)
            for item in self.repositories.jobs.load(job.project_id).jobs
        )
        solve_interval = derive_scene_solve_interval(
            clips=clips_manifest.clips,
            clip_id=clip.clip_id,
            source_frame_index=_project_source_frame_index(
                clips_manifest.clips, stored_jobs
            ),
        )
        video_path = _clip_asset_path(clip, project.source_assets, "video")
        if video_path is None or not video_path.is_file():
            raise FileNotFoundError("physical MP4 source is missing")
        attempt = Path(job.attempts[-1].directory)
        attempt.mkdir(parents=True, exist_ok=True)
        manifest_path = attempt / "solve_export_manifest.json"
        output_dir = attempt / "solve_inputs"
        interval = _solve_interval_payload(clip, solve_interval)
        manifest_path.write_text(
            json.dumps(
                {"clips": [{"clip_id": clip.clip_id, **interval}]},
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
            "--preset",
            "veryfast",
            "--max-duration-seconds",
            "120",
            "--allow-subset",
        )
        return JobExecutionPlan(
            commands=(command,),
            validate=lambda: _validate_solve_export_outputs(
                clip, solve_interval, output_dir
            ),
        )

    def _prepare_analysis(self, job: QueueJob) -> JobExecutionPlan:
        project = self.repositories.project.load(job.project_id)
        if job.job_type == "cad_replacement":
            state = project.source_assets.get("_cad_replacement")
            candidate = state.get("candidate") if isinstance(state, Mapping) else None
            if not isinstance(candidate, Mapping):
                raise ValueError("CAD replacement candidate is unavailable")
            return prepare_analysis_plan(job, {"cad": candidate})
        return prepare_analysis_plan(job, project.source_assets)

    def _attempt_directory(self, project_id: str, job_id: str, number: int) -> Path:
        return self.projects_root / project_id / "jobs" / job_id / f"attempt-{number}"

    @contextmanager
    def _state_guard(self, project_id: str) -> Iterator[None]:
        """Serialize project, clip, and job snapshots before acquiring queue state."""
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


def _require_owned_deletion_target(path: Path, root: Path) -> None:
    resolved = path.resolve(strict=False)
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"refusing to delete path outside owned workspace: {path}")


def _has_active_workbench_session(
    sessions_root: Path,
    *,
    project_id: str,
    now: str,
) -> bool:
    if not sessions_root.is_dir():
        return False
    try:
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        current = None
    for path in sessions_root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or payload.get("project_id") != project_id:
            continue
        state = payload.get("state")
        if state in {"pending_save", "recovery_required"}:
            return True
        if state != "editing":
            continue
        expires_at = payload.get("expires_at")
        if current is None or not isinstance(expires_at, str):
            return True
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if current < expiry:
            return True
    return False


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


def _uses_scene_solve_export(
    clip: ClipDefinition, clips: Sequence[ClipDefinition]
) -> bool:
    if clip.resolved_workflow != "sfm_only":
        return False
    scene_index = clip.analysis.get("scene_index")
    return (
        isinstance(scene_index, int)
        and not isinstance(scene_index, bool)
        and sum(
            1
            for candidate in clips
            if candidate.analysis.get("scene_index") == scene_index
        )
        > 1
    )


def _project_source_frame_index(
    clips: Sequence[ClipDefinition], jobs: Sequence[QueueJob]
) -> DecodedFrameIndex:
    """由核心 frame map 重建整段视频的权威 decoded-frame PTS 索引。"""

    if not clips:
        raise ValueError("project has no clips")
    ordered = sorted(
        clips,
        key=lambda clip: (
            int(clip.analysis.get("render_order", 0)),
            int(clip.analysis.get("source_start_pts", 0)),
            clip.clip_id,
        ),
    )
    time_base = _fraction_time_base(ordered[0])
    by_ordinal: dict[int, int] = {}
    for clip in ordered:
        if _fraction_time_base(clip) != time_base:
            raise ValueError("project clips use different source time bases")
        _video, frame_map = _render_physical_inputs(clip, jobs)
        for frame in _load_authoritative_source_frames(clip, frame_map):
            previous = by_ordinal.setdefault(frame.ordinal, frame.pts)
            if previous != frame.pts:
                raise ValueError("project frame maps disagree on source PTS")
    ordinals = sorted(by_ordinal)
    if ordinals != list(range(len(ordinals))):
        raise ValueError("project frame maps do not cover contiguous source ordinals")
    final_end = int(ordered[-1].analysis["source_end_pts_exclusive"])
    frames = tuple(
        DecodedFrameTimestamp(
            ordinal=ordinal,
            pts=by_ordinal[ordinal],
            duration_pts=(
                by_ordinal[ordinal + 1] - by_ordinal[ordinal]
                if ordinal + 1 < len(ordinals)
                else final_end - by_ordinal[ordinal]
            ),
            timestamp_source="pts",
        )
        for ordinal in ordinals
    )
    return DecodedFrameIndex(time_base, frames)


def _core_frame_map_identity(
    clip: ClipDefinition, jobs: Sequence[QueueJob]
) -> dict[str, object]:
    _video, frame_map = _render_physical_inputs(clip, jobs)
    if not frame_map.is_file():
        raise ValueError(f"authoritative frame map is unavailable: {clip.clip_id}")
    identity: dict[str, object] = {
        "sha256": sha256(frame_map.read_bytes()).hexdigest(),
        "size_bytes": frame_map.stat().st_size,
    }
    resolved = frame_map.resolve(strict=True)
    export = next(
        (
            job
            for job in reversed(tuple(jobs))
            if job.job_type == "clip_export"
            and job.clip_id == clip.clip_id
            and _has_exact_success_proof(job)
            and isinstance(
                job.published_outputs.get(f"frame_map:{clip.clip_id}"), str
            )
            and Path(
                str(job.published_outputs[f"frame_map:{clip.clip_id}"])
            ).resolve(strict=True)
            == resolved
        ),
        None,
    )
    if export is not None:
        identity["export_job_id"] = export.job_id
        identity["export_output_revision"] = export.output_revision
        identity["export_output_fingerprint"] = export.output_fingerprint
    return identity


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
    storage_root: Path,
    project_id: str,
    clip: ClipDefinition,
    project_assets: Mapping[str, object] | None = None,
) -> dict[str, object]:
    active = project_assets.get("cad") if isinstance(project_assets, Mapping) else None
    snapshot = clip.analysis.get("input_snapshot")
    cad = (
        active
        if isinstance(active, Mapping) and active.get("dataset_path")
        else (snapshot.get("cad") if isinstance(snapshot, Mapping) else None)
    )
    cad_path = cad.get("dataset_path") if isinstance(cad, Mapping) else None
    parameters: dict[str, object] = {}
    if isinstance(cad_path, str) and cad_path:
        parameters["cad_dataset_path"] = cad_path
    defaults: Mapping[str, object] = {}
    manifest_paths: list[Path] = []
    if isinstance(cad_path, str) and cad_path:
        manifest_paths.append(Path(cad_path) / "dataset_manifest.json")
    manifest_paths.append(
        Path(storage_root)
        / "data"
        / f"{project_id}-{clip.clip_id}"
        / "dataset_manifest.json"
    )
    for manifest_path in manifest_paths:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, Mapping) and isinstance(
                payload.get("defaults"), Mapping
            ):
                defaults = payload["defaults"]
                break
        except (OSError, json.JSONDecodeError):
            continue
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
        if not path.is_file():
            return False
        proof = job.validation_proof
        if not isinstance(proof, Mapping) or "trajectory_sha256" not in proof:
            return sha256(path.read_bytes()).hexdigest() == job.output_fingerprint
        bindings = {
            "trajectory": "trajectory_sha256",
            "solve_trajectory": "solve_trajectory_sha256",
            "solve_frame_map": "solve_frame_map_sha256",
            "core_frame_map": "core_frame_map_sha256",
        }
        for output_key, proof_key in bindings.items():
            raw = job.published_outputs.get(output_key)
            expected = proof.get(proof_key)
            if not isinstance(raw, str) or not isinstance(expected, str):
                return False
            artifact = Path(raw).resolve(strict=True)
            if not artifact.is_file() or sha256(artifact.read_bytes()).hexdigest() != expected:
                return False
        return True
    except (OSError, ValueError):
        return False


def _has_precomputed_solve_outputs(job: QueueJob) -> bool:
    if job.adapter_name != "sfm_only" or job.adapter_version != "2":
        return False
    try:
        return all(
            isinstance(job.published_outputs.get(key), str)
            and Path(str(job.published_outputs[key])).is_file()
            for key in (
                "trajectory",
                "solve_trajectory",
                "solve_video",
                "solve_frame_map",
                "core_frame_map",
            )
        )
    except OSError:
        return False


def _saved_workbench_reference(clip: ClipDefinition) -> StateReference | None:
    for reference in reversed(clip.references):
        value = reference.value
        if (
            reference.owner == "clips"
            and reference.key == f"workbench:{clip.clip_id}"
            and value.get("status") in {"saved", "editing", "pending_save"}
            and isinstance(value.get("workbench_output_revision"), str)
            and isinstance(value.get("workbench_output_fingerprint"), str)
        ):
            return reference
    return None


def _has_recoverable_saved_workbench_output(
    projects_root: Path,
    project_id: str,
    clip: ClipDefinition,
    trajectory: QueueJob | None,
) -> bool:
    if trajectory is None:
        return False
    sessions = projects_root / project_id / "workbench_sessions"
    if not sessions.is_dir():
        return False
    for path in sessions.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("state") != "saved"
            or payload.get("project_id") != project_id
            or payload.get("clip_id") != clip.clip_id
            or payload.get("workflow") != clip.resolved_workflow
            or payload.get("clip_input_revision") != clip.analysis_revision
        ):
            continue
        revision = payload.get("workbench_output_revision")
        fingerprint = payload.get("workbench_output_fingerprint")
        operation_id = payload.get("workbench_output_operation_id") or payload.get(
            "operation_id"
        )
        if not all(isinstance(value, str) and value for value in (
            revision,
            fingerprint,
            operation_id,
        )):
            continue
        reference = StateReference(
            owner="clips",
            key=f"workbench:{clip.clip_id}",
            operation_id=str(operation_id),
            value={
                "status": "saved",
                "workflow": payload.get("workflow"),
                "input_revision": payload.get("clip_input_revision"),
                "input_fingerprint": payload.get("input_fingerprint"),
                "trajectory_job_id": payload.get("trajectory_job_id"),
                "trajectory_output_revision": payload.get(
                    "trajectory_output_revision"
                ),
                "trajectory_output_fingerprint": payload.get(
                    "trajectory_output_fingerprint"
                ),
                "workbench_output_revision": revision,
                "workbench_output_fingerprint": fingerprint,
                "workbench_output_operation_id": operation_id,
            },
        )
        if _workbench_binds_trajectory(
            clip, trajectory, reference.value
        ) and _validate_workbench_immutable_output(
            projects_root, project_id, clip, reference
        ):
            return True
    return False


def _workbench_reference_blocks_scene_bridge(
    reference: StateReference, now: str
) -> bool:
    if reference.value.get("status") != "editing":
        return True
    expires_at = reference.value.get("expires_at")
    if not isinstance(expires_at, str):
        return True
    try:
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return current < expiry


def _validate_scene_bridge_reference(
    projects_root: Path,
    project_id: str,
    clip: ClipDefinition,
    reference: StateReference,
) -> bool:
    revision = reference.value.get("bridge_revision")
    fingerprint = reference.value.get("output_fingerprint")
    job_id = reference.value.get("job_id")
    if (
        reference.owner != "jobs"
        or reference.key != f"job:{job_id}"
        or reference.value.get("reference_type") != "scene_bridge"
        or reference.value.get("status") != "awaiting_route_refinement"
        or not isinstance(revision, str)
        or not isinstance(fingerprint, str)
    ):
        return False
    root = projects_root / project_id / "scene_bridges" / clip.clip_id / revision
    try:
        manifest = json.loads(
            (root / "scene_bridge_manifest.json").read_text(encoding="utf-8-sig")
        )
        identity = manifest.get("identity")
        if not isinstance(identity, Mapping):
            return False
        validated = validate_scene_bridge_candidate(root, identity)
        return bool(
            validated.status == "success"
            and validated.output_revision == revision
            and validated.output_fingerprint == fingerprint
            and identity.get("project_id") == project_id
            and identity.get("target_clip_id") == clip.clip_id
            and identity.get("source_clip_id")
            == reference.value.get("source_clip_id")
            and identity.get("direction") == reference.value.get("direction")
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


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
            "operation_id": workbench.value.get(
                "workbench_output_operation_id", workbench.operation_id
            ),
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
    annotation_identity: Mapping[str, object] | None = None,
    active_cad_identity: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    return {
        "job_type": "clip_render",
        "clip_id": clip.clip_id,
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
            "output_operation_id": workbench.value.get(
                "workbench_output_operation_id", workbench.operation_id
            ),
            "output_revision": workbench.value.get("workbench_output_revision"),
            "output_fingerprint": workbench.value.get(
                "workbench_output_fingerprint"
            ),
        },
        "project_media_spec_revision": media_spec_revision,
        "project_media_spec": media_spec.to_dict(),
        "adapter_name": adapter_name,
        "adapter_version": adapter_version,
        "annotation_dependencies": dict(annotation_identity or {}),
        "active_cad": dict(active_cad_identity or {}),
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


def _upload_descriptor(upload: PublishedUpload) -> dict[str, object]:
    return {
        "path": str(upload.path),
        "original_filename": upload.original_filename,
        "size_bytes": upload.size_bytes,
        "sha256": upload.sha256,
        "validation": dict(upload.validation),
        "validation_report": str(upload.validation_report_path),
    }


def _cad_replacement_identity_payload(
    *,
    project_id: str,
    candidate_revision: str,
    candidate: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema": 1,
        "job_type": "cad_replacement",
        "project_id": project_id,
        "candidate_revision": candidate_revision,
        "candidate": {
            "path": candidate.get("path"),
            "sha256": candidate.get("sha256"),
            "size_bytes": candidate.get("size_bytes"),
            "original_filename": candidate.get("original_filename"),
        },
        "adapter_name": ANALYSIS_ADAPTER_NAME,
        "adapter_version": ANALYSIS_ADAPTER_VERSION,
    }


def _cad_descriptor_revision(descriptor: Mapping[str, object]) -> str:
    digest = descriptor.get("sha256")
    if isinstance(digest, str) and digest:
        return f"cad-upload:{digest[:16]}"
    dataset_id = descriptor.get("dataset_id")
    if isinstance(dataset_id, str) and dataset_id:
        return dataset_id
    return f"cad-legacy:{_fingerprint(dict(descriptor))[:16]}"


def _complete_active_cad_descriptor(
    descriptor: dict[str, object], clips: ClipsManifest
) -> dict[str, object]:
    """兼容旧项目：首次替换时从不可变 clip 快照补齐 CAD 数据集身份。"""

    if descriptor.get("dataset_path") and descriptor.get("dataset_id"):
        return descriptor
    for clip in clips.clips:
        snapshot = clip.analysis.get("input_snapshot")
        cad = snapshot.get("cad") if isinstance(snapshot, Mapping) else None
        if not isinstance(cad, Mapping):
            continue
        active_sha = descriptor.get("sha256")
        snapshot_sha = cad.get("sha256")
        if (
            isinstance(active_sha, str)
            and active_sha
            and isinstance(snapshot_sha, str)
            and snapshot_sha
            and active_sha != snapshot_sha
        ):
            continue
        for key in ("dataset_id", "dataset_path"):
            if key not in descriptor and cad.get(key) is not None:
                descriptor[key] = cad[key]
        if descriptor.get("dataset_path"):
            break
    return descriptor


def _complete_cad_georeference_assets(
    project_assets: Mapping[str, object], clips: ClipsManifest
) -> Mapping[str, object]:
    active_cad = project_assets.get("cad")
    if not isinstance(active_cad, Mapping):
        return project_assets
    return {
        **project_assets,
        "cad": _complete_active_cad_descriptor(dict(active_cad), clips),
    }


def _active_cad_render_identity(
    project_assets: Mapping[str, object], clip: ClipDefinition
) -> dict[str, object]:
    active = project_assets.get("cad")
    snapshot = clip.analysis.get("input_snapshot")
    fallback = snapshot.get("cad") if isinstance(snapshot, Mapping) else None
    cad = active if isinstance(active, Mapping) else fallback
    if not isinstance(cad, Mapping):
        return {}
    return {
        key: cad.get(key)
        for key in ("revision", "sha256", "dataset_id", "dataset_path")
        if cad.get(key) is not None
    }


def _cad_georeference_candidate_request(
    project_id: str,
    project_assets: Mapping[str, object],
    *,
    central_meridian_deg: object | None,
    limit: int,
) -> dict[str, object]:
    requested_meridian = parse_central_meridian(central_meridian_deg)
    candidate_limit = int(limit)
    if candidate_limit <= 0:
        raise ValueError("candidate limit must be positive")
    srt_path = _asset_path(project_assets, "srt")
    if srt_path is None or not srt_path.is_file():
        raise FileNotFoundError("physical SRT is required for CAD georeference")
    srt_descriptor = project_assets.get("srt")
    if isinstance(srt_descriptor, Mapping):
        srt_identity: Mapping[str, object] = {
            key: srt_descriptor.get(key)
            for key in ("revision", "sha256", "path", "size_bytes")
            if srt_descriptor.get(key) is not None
        }
    else:
        stat = srt_path.stat()
        srt_identity = {
            "path": str(srt_path.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    cad_fingerprint = _active_cad_asset_fingerprint(project_assets)
    if cad_fingerprint is None:
        raise ValueError("active CAD asset identity is unavailable")
    return {
        "schema_version": 1,
        "algorithm_version": CAD_GEOREFERENCE_CANDIDATE_ADAPTER_VERSION,
        "project_id": project_id,
        "cad_asset_fingerprint": cad_fingerprint,
        "srt_asset_fingerprint": _fingerprint(dict(srt_identity)),
        "srt_path": str(srt_path.resolve()),
        "cad_bbox_raw": list(_active_cad_coordinate_bbox(project_assets)),
        "central_meridian_deg": requested_meridian,
        "limit": candidate_limit,
    }


def _active_cad_asset_fingerprint(
    project_assets: Mapping[str, object],
) -> str | None:
    cad = project_assets.get("cad")
    if not isinstance(cad, Mapping):
        path = project_assets.get("cad_path")
        if not isinstance(path, str) or not path:
            return None
        return _fingerprint({"path": path})
    identity = {
        key: cad.get(key)
        for key in ("revision", "sha256", "dataset_id", "dataset_path", "path")
        if cad.get(key) is not None
    }
    return _fingerprint(identity) if identity else None


def _active_cad_coordinate_bbox(
    project_assets: Mapping[str, object],
) -> tuple[float, float, float, float]:
    cad = project_assets.get("cad")
    if not isinstance(cad, Mapping):
        raise ValueError("active CAD descriptor is unavailable")
    focus_candidates: list[object] = [cad.get("viewer_focus_bbox")]
    fallback_candidates: list[object] = [
        cad.get("coordinate_bbox"),
        cad.get("bbox"),
    ]
    stats = cad.get("stats")
    if isinstance(stats, Mapping):
        focus_candidates.append(stats.get("viewer_focus_bbox"))
        fallback_candidates.extend(
            (stats.get("coordinate_bbox"), stats.get("bbox"))
        )
    dataset_path = cad.get("dataset_path")
    if isinstance(dataset_path, str) and dataset_path:
        try:
            import_stats = json.loads(
                (Path(dataset_path) / "cad_import_stats.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            if isinstance(import_stats, Mapping):
                focus_candidates.append(import_stats.get("viewer_focus_bbox"))
                fallback_candidates.extend(
                    (
                        import_stats.get("coordinate_bbox"),
                        import_stats.get("bbox"),
                    )
                )
        except (OSError, json.JSONDecodeError):
            pass
        try:
            manifest = json.loads(
                (Path(dataset_path) / "dataset_manifest.json").read_text(
                    encoding="utf-8-sig"
                )
            )
            manifest_cad = (
                manifest.get("cad") if isinstance(manifest, Mapping) else None
            )
            if isinstance(manifest_cad, Mapping):
                focus_candidates.append(manifest_cad.get("viewer_focus_bbox"))
                fallback_candidates.extend(
                    (
                        manifest_cad.get("coordinate_bbox"),
                        manifest_cad.get("bbox"),
                    )
                )
        except (OSError, json.JSONDecodeError):
            pass
    for candidate in (*focus_candidates, *fallback_candidates):
        if (
            isinstance(candidate, Sequence)
            and not isinstance(candidate, (str, bytes))
            and len(candidate) == 4
        ):
            return tuple(float(value) for value in candidate)  # type: ignore[return-value]
    raise ValueError("active CAD coordinate bbox is unavailable")


def _confirmed_cad_georeference(
    project_assets: Mapping[str, object],
) -> Mapping[str, object] | None:
    value = project_assets.get("_cad_georeference")
    if not isinstance(value, Mapping) or value.get("confirmed") is not True:
        return None
    if value.get("cad_asset_fingerprint") != _active_cad_asset_fingerprint(
        project_assets
    ):
        return None
    try:
        CadGeoreference.from_dict(value)
    except (TypeError, ValueError):
        return None
    return value


def _cad_georeference_snapshot(
    project_assets: Mapping[str, object],
) -> Mapping[str, object]:
    value = project_assets.get("_cad_georeference")
    if not isinstance(value, Mapping):
        return {}
    if _confirmed_cad_georeference(project_assets) is not None:
        return dict(value)
    return {
        **value,
        "confirmed": False,
        "stale_reason": "cad_asset_changed",
    }


def _srt_full_pose_adapter_parameters(
    storage_root: Path,
    project: ProjectManifest,
    clip: ClipDefinition,
) -> dict[str, object]:
    georeference = _confirmed_cad_georeference(project.source_assets)
    settings = clip.manual_definition.get("srt_full_pose")
    media_binding = _project_media_binding(project)
    if georeference is None:
        raise ValueError("confirmed CAD georeference is not bound to the current CAD")
    if not isinstance(settings, Mapping):
        raise ValueError("srt_full_pose settings are unavailable")
    if media_binding is None:
        raise ValueError("project media specification is unavailable")
    _media_revision, media = media_binding
    if media.nominal_frame_rate is None:
        raise ValueError("project nominal frame rate is unavailable")
    render = _workbench_render_parameters(
        storage_root, project.project_id, clip, project.source_assets
    )
    return {
        "cad_georeference": dict(georeference),
        "srt_full_pose": dict(settings),
        "cad_origin_xy": list(render["origin_xy"]),
        "cad_scale": render["cad_scale"],
        "video_metadata": {
            "width": media.width,
            "height": media.height,
            "fps": float(media.nominal_frame_rate),
        },
    }


def _fixed_track_visual_pose_adapter_parameters(
    storage_root: Path,
    project: ProjectManifest,
    clip: ClipDefinition,
) -> dict[str, object]:
    georeference = _confirmed_cad_georeference(project.source_assets)
    settings = clip.manual_definition.get("srt_fixed_track_visual_pose")
    media_binding = _project_media_binding(project)
    if georeference is None:
        raise ValueError("confirmed CAD georeference is not bound to the current CAD")
    if not isinstance(settings, Mapping):
        raise ValueError("srt_fixed_track_visual_pose settings are unavailable")
    if media_binding is None:
        raise ValueError("project media specification is unavailable")
    _media_revision, media = media_binding
    if media.nominal_frame_rate is None:
        raise ValueError("project nominal frame rate is unavailable")
    render = _workbench_render_parameters(
        storage_root, project.project_id, clip, project.source_assets
    )
    return {
        "cad_georeference": dict(georeference),
        "srt_fixed_track_visual_pose": dict(settings),
        "cad_origin_xy": list(render["origin_xy"]),
        "cad_scale": render["cad_scale"],
        "video_metadata": {
            "width": media.width,
            "height": media.height,
            "fps": float(media.nominal_frame_rate),
        },
    }


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
    if adapter_name in {"srt_full_pose", "srt_fixed_track_visual_pose"}:
        payload["cad_georeference"] = _cad_georeference_snapshot(project_assets)
    if _clip_input_snapshot(clip) is None:
        payload.update(
            {
                "project_manifest_revision": project_revision,
                "clips_manifest_revision": clips_revision,
            }
        )
    return payload


def _same_scene_adjacent_clip(
    clips: Sequence[ClipDefinition], source: ClipDefinition, direction: str
) -> ClipDefinition | None:
    scene_index = int(source.analysis.get("scene_index", 1))
    same_scene = sorted(
        (
            clip
            for clip in clips
            if int(clip.analysis.get("scene_index", 1)) == scene_index
        ),
        key=lambda clip: (
            int(clip.analysis.get("segment_index", 1)),
            int(clip.analysis.get("render_order", 0)),
            clip.clip_id,
        ),
    )
    source_index = next(
        (
            index
            for index, candidate in enumerate(same_scene)
            if candidate.clip_id == source.clip_id
        ),
        None,
    )
    if source_index is None:
        return None
    target_index = source_index - 1 if direction == "up" else source_index + 1
    return same_scene[target_index] if 0 <= target_index < len(same_scene) else None


def _scene_bridge_identity_payload(
    *,
    project: ProjectManifest,
    source: ClipDefinition,
    target: ClipDefinition,
    source_workbench: StateReference,
    source_trajectory: QueueJob,
    target_trajectory: QueueJob,
    direction: str,
    source_core_frame_map: Mapping[str, object],
    target_core_frame_map: Mapping[str, object],
) -> dict[str, object]:
    return {
        "identity_schema": 1,
        "algorithm_version": BRIDGE_ALGORITHM_VERSION,
        "project_id": project.project_id,
        "source_clip_id": source.clip_id,
        "target_clip_id": target.clip_id,
        "direction": direction,
        "overlap_seconds": "4",
        "source_analysis_revision": source.analysis_revision,
        "target_analysis_revision": target.analysis_revision,
        "source_interval": _authoritative_interval(source),
        "target_interval": _authoritative_interval(target),
        "source_assets": _clip_input_identity(source, project.source_assets),
        "target_assets": _clip_input_identity(target, project.source_assets),
        "source_core_frame_map": dict(source_core_frame_map),
        "target_core_frame_map": dict(target_core_frame_map),
        "active_cad": _active_cad_render_identity(project.source_assets, target),
        "source_workbench_output_revision": source_workbench.value.get(
            "workbench_output_revision"
        ),
        "source_workbench_output_fingerprint": source_workbench.value.get(
            "workbench_output_fingerprint"
        ),
        "source_trajectory": {
            "job_id": source_trajectory.job_id,
            "input_fingerprint": source_trajectory.input_fingerprint,
            "output_revision": source_trajectory.output_revision,
            "output_fingerprint": source_trajectory.output_fingerprint,
        },
        "target_trajectory": {
            "job_id": target_trajectory.job_id,
            "input_fingerprint": target_trajectory.input_fingerprint,
        },
    }


def _scene_bridge_request_matches_job(
    payload: object, job: QueueJob, path: Path
) -> bool:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        return False
    operation_id = payload.get("operation_id")
    identity = payload.get("identity")
    runner_identity = payload.get("runner_identity")
    if (
        not isinstance(operation_id, str)
        or path.name != f"{operation_id}.json"
        or not isinstance(identity, Mapping)
        or not isinstance(runner_identity, Mapping)
        or identity.get("project_id") != job.project_id
        or identity.get("target_clip_id") != job.clip_id
        or runner_identity.get("operation_id") != operation_id
        or any(runner_identity.get(key) != value for key, value in identity.items())
    ):
        return False
    return _fingerprint(identity) == job.input_fingerprint


def _atomic_write_json_file(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def _solve_interval_payload(
    clip: ClipDefinition, solve_interval: SolveInterval
) -> dict[str, object]:
    time_base = _fraction_time_base(clip)
    return {
        "source_start_pts": solve_interval.start_pts,
        "source_end_pts_exclusive": solve_interval.end_pts_exclusive,
        "source_time_base": {
            "numerator": time_base.numerator,
            "denominator": time_base.denominator,
        },
        "interval_semantics": "half_open",
        "core_start_pts": solve_interval.core_start_pts,
        "core_end_pts_exclusive": solve_interval.core_end_pts_exclusive,
        "core_start_index": solve_interval.core_start_index,
        "core_end_index_exclusive": solve_interval.core_end_index_exclusive,
        "frame_pts": list(solve_interval.frame_pts),
    }


def _solve_export_identity_payload(
    *,
    clip: ClipDefinition,
    solve_interval: SolveInterval,
    project_assets: Mapping[str, object],
    project_revision: int,
    clips_revision: int,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "job_type": "sfm_solve_export",
        "clip_id": clip.clip_id,
        "analysis_revision": clip.analysis_revision,
        "core_interval": _authoritative_interval(clip),
        "solve_interval": _solve_interval_payload(clip, solve_interval),
        "source_assets": _clip_input_identity(clip, project_assets),
        "algorithm_version": "same-scene-overlap-4s-v1",
        "adapter_name": "sfm_solve_export",
        "adapter_version": "1",
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


def _validate_solve_export_outputs(
    clip: ClipDefinition,
    solve_interval: SolveInterval,
    output_dir: Path,
) -> AdapterResult:
    frame_map_path = output_dir / "clip_frame_map.json"
    video_path = output_dir / f"{clip.clip_id}.mp4"
    if not frame_map_path.is_file():
        return AdapterResult.failed("solve export frame map is missing")
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return AdapterResult.failed("solve export video is missing")
    try:
        payload = json.loads(frame_map_path.read_text(encoding="utf-8"))
        raw_clips = payload.get("clips") if isinstance(payload, Mapping) else None
        item = next(
            (
                value
                for value in raw_clips or ()
                if isinstance(value, Mapping)
                and value.get("clip_id") == clip.clip_id
            ),
            None,
        )
    except (OSError, json.JSONDecodeError) as exc:
        return AdapterResult.failed(f"invalid solve export frame map: {exc}")
    if (
        item is None
        or item.get("source_start_pts") != solve_interval.start_pts
        or item.get("source_end_pts_exclusive")
        != solve_interval.end_pts_exclusive
    ):
        return AdapterResult.failed("solve export frame map interval mismatch")
    digest = sha256(frame_map_path.read_bytes())
    digest.update(video_path.read_bytes())
    fingerprint = digest.hexdigest()
    return AdapterResult.success(
        output_revision=f"sfm_solve_export:{fingerprint[:16]}",
        output_fingerprint=fingerprint,
        outputs={
            f"solve_video:{clip.clip_id}": str(video_path),
            f"solve_frame_map:{clip.clip_id}": str(frame_map_path),
        },
    )
