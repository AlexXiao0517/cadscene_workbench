from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import ExitStack
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import BinaryIO, Callable, Mapping
from uuid import uuid4

from .json_repositories import ProjectRepositories
from .identifiers import is_safe_stable_id, validate_project_id
from .models import ClipDefinition, ProjectManifest, StateReference
from .repositories import RevisionConflict
from .service import ProjectService, RenderPreflight, TrajectoryPreflight
from .uploads import UploadValidationError, ValidatedUploadStore
from .workbench_sessions import (
    InvalidWorkbenchOutput,
    InvalidWorkbenchReturnPath,
    ProjectWorkbenchService,
    ReplayedWorkbenchSave,
    StaleWorkbenchSession,
    WorkbenchPermissionDenied,
)


_SAFE_ID = r"[A-Za-z0-9_.-]+"
_PROJECT = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})$")
_UPLOAD = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/uploads/(?P<asset>video|cad|srt)$"
)
_CAD_REPLACEMENT_UPLOAD = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/uploads/cad-replacement$"
)
_ANALYSIS = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/analysis/start$")
_ANALYSIS_ACTIVATE = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/analysis/activate$"
)
_SNAPSHOT = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/snapshot$")
_WORKFLOW = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/workflow$"
)
_NAME = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/name$"
)
_TRAJECTORY = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/trajectory-jobs$")
_RENDER = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/render-jobs$")
_MERGE = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/merge-jobs$")
_ANNOTATIONS = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/annotations$")
_ANNOTATION = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/annotations/(?P<annotation>{_SAFE_ID})$"
)
_ANNOTATION_TRACKING = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/annotations/(?P<annotation>{_SAFE_ID})/(?P<action>track|tracking)$"
)
_ANNOTATION_PREVIEW = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/annotation-preview$"
)
_JOB_ACTION = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/jobs/(?P<job>{_SAFE_ID})/(?P<action>retry|cancel)$"
)
_JOB_RUNTIME = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/jobs/(?P<job>{_SAFE_ID})/runtime$"
)
_WORKBENCH_CREATE = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/workbench-sessions$"
)
_ADJACENT_LOCATE = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/locate-adjacent$"
)
_SCENE_BRIDGE = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/scene-bridges$"
)
_WORKBENCH_SESSION = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/workbench-sessions/(?P<token>[A-Za-z0-9_-]+)(?:/(?P<action>save|close|heartbeat|trajectory-ready|resume))?$"
)


@dataclass(frozen=True)
class UploadRequest:
    filename: str
    stream: BinaryIO
    size_bytes: int
    sha256: str | None = None


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Mapping[str, object] | None = None
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", dict(self.headers or {}))

    @property
    def encoded_body(self) -> bytes:
        if self.body is None:
            return b""
        return json.dumps(self.body, ensure_ascii=False).encode("utf-8")


class ProjectApi:
    """Route parsing, validation and project-service delegation boundary."""

    def __init__(
        self,
        *,
        repositories: ProjectRepositories,
        service: ProjectService,
        uploads: ValidatedUploadStore,
        now: Callable[[], str],
        identity: Callable[[], str] | None = None,
        workbench: ProjectWorkbenchService | None = None,
    ) -> None:
        self.repositories = repositories
        self.service = service
        self.uploads = uploads
        self.now = now
        self._identity = identity or (lambda: uuid4().hex)
        self.workbench = workbench

    def handle(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        upload: UploadRequest | None = None,
    ) -> ApiResponse:
        headers = {str(key).lower(): value for key, value in (headers or {}).items()}
        payload = json_body or {}
        try:
            if method == "GET" and path == "/api/projects":
                return ApiResponse(
                    200,
                    {"projects": [item.to_dict() for item in self.service.list_projects()]},
                    {"Cache-Control": "no-store"},
                )
            if method == "POST" and path == "/api/projects":
                return self._create_project(payload)
            match = _PROJECT.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_project_name(match["project"], payload)
            match = _SNAPSHOT.fullmatch(path)
            if method == "GET" and match:
                return self._snapshot(match["project"], headers)
            match = _ANNOTATION_PREVIEW.fullmatch(path)
            if method == "GET" and match:
                return ApiResponse(
                    200,
                    self.service.annotation_preview_timing(
                        match["project"], match["clip"]
                    ),
                    {"Cache-Control": "no-store"},
                )
            match = _CAD_REPLACEMENT_UPLOAD.fullmatch(path)
            if method == "POST" and match:
                if upload is None:
                    raise ValueError("project upload body is required")
                return self._publish_cad_replacement(
                    match["project"], payload, upload
                )
            match = _UPLOAD.fullmatch(path)
            if method == "POST" and match:
                if upload is None:
                    raise ValueError("project upload body is required")
                return self._publish_upload(
                    match["project"], match["asset"], payload, upload
                )
            match = _ANALYSIS.fullmatch(path)
            if method == "POST" and match:
                return self._start_analysis(match["project"], payload)
            match = _ANALYSIS_ACTIVATE.fullmatch(path)
            if method == "POST" and match:
                return self._activate_analysis(match["project"], payload)
            match = _WORKFLOW.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_workflow(match["project"], match["clip"], payload)
            match = _NAME.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_name(match["project"], match["clip"], payload)
            match = _TRAJECTORY.fullmatch(path)
            if method == "POST" and match:
                return self._trajectory_jobs(match["project"], payload)
            match = _RENDER.fullmatch(path)
            if method == "POST" and match:
                return self._render_jobs(match["project"], payload)
            match = _MERGE.fullmatch(path)
            if method == "POST" and match:
                return self._merge_job(match["project"], payload)
            match = _ANNOTATIONS.fullmatch(path)
            if method == "POST" and match:
                return self._create_annotation(match["project"], payload)
            match = _ANNOTATION_TRACKING.fullmatch(path)
            if method == "POST" and match and match["action"] == "track":
                return self._track_annotation(
                    match["project"], match["annotation"], payload
                )
            if method == "GET" and match and match["action"] == "tracking":
                return self._annotation_tracking(match["project"], match["annotation"])
            match = _ANNOTATION.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_annotation(
                    match["project"], match["annotation"], payload
                )
            if method == "DELETE" and match:
                return self._delete_annotation(
                    match["project"], match["annotation"], payload
                )
            match = _JOB_RUNTIME.fullmatch(path)
            if method == "GET" and match:
                return ApiResponse(
                    200,
                    self.service.job_runtime(match["project"], match["job"]),
                    {"Cache-Control": "no-store"},
                )
            match = _JOB_ACTION.fullmatch(path)
            if method == "POST" and match:
                return self._job_action(
                    match["project"], match["job"], match["action"], payload
                )
            match = _ADJACENT_LOCATE.fullmatch(path)
            if method == "POST" and match:
                return self._locate_adjacent_clip(
                    match["project"], match["clip"], payload
                )
            match = _SCENE_BRIDGE.fullmatch(path)
            if method == "POST" and match:
                return self._enqueue_scene_bridge(
                    match["project"], match["clip"], payload
                )
            match = _WORKBENCH_CREATE.fullmatch(path)
            if method == "POST" and match:
                return self._create_workbench_session(
                    match["project"], match["clip"], payload
                )
            match = _WORKBENCH_SESSION.fullmatch(path)
            if match and method == "GET" and match["action"] is None:
                return self._inspect_workbench_session(match["project"], match["token"])
            if match and method == "POST" and match["action"] in {"save", "close"}:
                return self._mutate_workbench_session(
                    match["project"], match["token"], match["action"], payload
                )
            if match and method == "POST" and match["action"] == "heartbeat":
                return self._heartbeat_workbench_session(
                    match["project"], match["token"], payload
                )
            if match and method == "POST" and match["action"] == "trajectory-ready":
                return self._attach_workbench_trajectory(
                    match["project"], match["token"], payload
                )
            if match and method == "POST" and match["action"] == "resume":
                return self._update_workbench_resume(
                    match["project"], match["token"], payload
                )
            return ApiResponse(404, {"error": "project_api_not_found"})
        except RevisionConflict as exc:
            return ApiResponse(
                409,
                {
                    "error": exc.code,
                    "expected_revision": exc.expected_revision,
                    "current_revision": exc.current_revision,
                },
            )
        except FileNotFoundError as exc:
            return ApiResponse(404, {"error": str(exc)})
        except WorkbenchPermissionDenied as exc:
            return ApiResponse(403, {"error": str(exc)})
        except ReplayedWorkbenchSave as exc:
            return ApiResponse(
                409, {"error": "workbench_save_replayed", "message": str(exc)}
            )
        except StaleWorkbenchSession as exc:
            return ApiResponse(
                409, {"error": "stale_workbench_session", "message": str(exc)}
            )
        except (UploadValidationError, ValueError, KeyError, TypeError) as exc:
            return ApiResponse(400, {"error": str(exc)})
        except (InvalidWorkbenchOutput, InvalidWorkbenchReturnPath) as exc:
            return ApiResponse(400, {"error": str(exc)})

    def _create_project(self, payload: Mapping[str, object]) -> ApiResponse:
        project_id = validate_project_id(
            str(payload.get("project_id") or f"project-{self._identity()}")
        )
        self.repositories.create_project(project_id, updated_at=self.now())
        display_name = str(payload.get("display_name") or "").strip()
        project_revision = 0
        if display_name:
            project = self.repositories.project.load(project_id)
            updated = self.repositories.project.update(
                project_id,
                expected_revision=project.revision,
                mutate=lambda current: replace(
                    current,
                    updated_at=self.now(),
                    source_assets={
                        **current.source_assets,
                        "display_name": display_name[:120],
                    },
                ),
            )
            project_revision = updated.revision
        return ApiResponse(
            201,
            {
                "project_id": project_id,
                "project_revision": project_revision,
                "workspace_url": f"/apps/project_workspace/?projectId={project_id}",
            },
        )

    def _update_project_name(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if not isinstance(payload.get("display_name"), str):
            raise TypeError("display_name must be a string")
        updated = self.service.update_project_display_name(
            project_id,
            expected_revision=_required_revision(payload),
            display_name=str(payload["display_name"]),
        )
        return ApiResponse(
            200,
            {
                "project_id": project_id,
                "project_revision": updated.revision,
                "display_name": str(updated.source_assets["display_name"]),
            },
        )

    def _publish_upload(
        self,
        project_id: str,
        asset_type: str,
        payload: Mapping[str, object],
        request: UploadRequest,
    ) -> ApiResponse:
        return self._publish_upload_locked(project_id, asset_type, payload, request)

    def _publish_cad_replacement(
        self,
        project_id: str,
        payload: Mapping[str, object],
        request: UploadRequest,
    ) -> ApiResponse:
        if payload.get("same_coordinate_system_confirmed") is not True:
            raise ValueError("必须确认新版 CAD 与当前项目使用相同坐标系")
        pending = self.uploads.begin(
            project_id,
            "cad",
            request.filename,
            expected_size=request.size_bytes,
            expected_sha256=request.sha256,
        )
        try:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                pending.write(chunk)
            published = pending.complete_staged()
        except BaseException:
            pending.abort()
            raise
        result = self.service.request_cad_replacement(
            project_id,
            published,
            expected_revision=_required_revision(payload),
            same_coordinate_system_confirmed=True,
        )
        return ApiResponse(
            202,
            {
                "project_id": project_id,
                "project_revision": result.project_revision,
                "job_id": result.job_id,
                "candidate_revision": result.candidate_revision,
                "status": "queued",
            },
        )

    def _publish_upload_locked(
        self,
        project_id: str,
        asset_type: str,
        payload: Mapping[str, object],
        request: UploadRequest,
    ) -> ApiResponse:
        use_current_revision = payload.get("use_current_revision") is True
        expected_revision = (
            None if use_current_revision else _required_revision(payload)
        )
        pending = self.uploads.begin(
            project_id,
            asset_type,
            request.filename,
            expected_size=request.size_bytes,
            expected_sha256=request.sha256,
        )
        try:
            while True:
                chunk = request.stream.read(1024 * 1024)
                if not chunk:
                    break
                pending.write(chunk)
            published = pending.complete_staged()
        except BaseException:
            pending.abort()
            raise
        registered = self.service.register_uploaded_asset(
            project_id,
            expected_revision=expected_revision,
            upload=published,
        )
        self.uploads.commit_canonical(published, strict=False)
        return ApiResponse(
            201,
            {
                "project_id": project_id,
                "asset_type": asset_type,
                "project_revision": registered.project_revision,
                "fingerprint": published.sha256,
                "validation": dict(published.validation),
                "workspace_url": f"/apps/project_workspace/?projectId={project_id}",
            },
        )

    def _start_analysis(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        expected_revision = _required_revision(payload)
        result = self.service.request_reanalysis(
            project_id, expected_revision=expected_revision
        )
        return ApiResponse(
            202,
            {
                "project_id": project_id,
                "triggered": ["video"],
                "project_revision": result.project_revision,
                "request_key": result.request_key,
                "job_ids": list(result.analysis_job_ids),
            },
        )

    def _activate_analysis(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        candidate_revision = payload.get("candidate_analysis_revision")
        if not isinstance(candidate_revision, str) or not candidate_revision:
            raise ValueError("candidate_analysis_revision must be a non-empty string")
        expected_clips_revision = payload.get("expected_clips_revision")
        if (
            isinstance(expected_clips_revision, bool)
            or not isinstance(expected_clips_revision, int)
            or expected_clips_revision < 0
        ):
            raise ValueError("expected_clips_revision must be a non-negative integer")
        result = self.service.activate_candidate_analysis(
            project_id,
            candidate_analysis_revision=candidate_revision,
            expected_project_revision=_required_revision(payload),
            expected_clips_revision=expected_clips_revision,
        )
        return ApiResponse(
            200,
            {
                "project_id": project_id,
                "active_analysis_revision": result.analysis_revision,
                "project_revision": result.project_revision,
                "clips_revision": result.clips_revision,
            },
        )

    def _snapshot(self, project_id: str, headers: Mapping[str, str]) -> ApiResponse:
        snapshot = self._build_snapshot(project_id)
        etag = f'"{snapshot["snapshot_revision"]}"'
        response_headers = {"ETag": etag, "Cache-Control": "no-store"}
        if headers.get("if-none-match") == etag:
            return ApiResponse(304, None, response_headers)
        return ApiResponse(200, snapshot, response_headers)

    def _create_annotation(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        from cadscene.annotations.models import (
            AnnotationContent,
            AnnotationLeader,
            AnnotationPanel,
            AnnotationStyle,
            SourcePtsRange,
            VisibilityPolicy,
        )

        result = self.service.annotation_service.create(
            project_id,
            expected_revision=int(payload["expected_revision"]),
            annotation_id=(
                None
                if payload.get("annotation_id") is None
                else str(payload["annotation_id"])
            ),
            clip_id=str(payload["clip_id"]),
            anchor_type=str(payload["anchor_type"]),
            text=str(payload.get("text", "")),
            content=(
                AnnotationContent.from_dict(payload["content"])
                if isinstance(payload.get("content"), Mapping)
                else None
            ),
            panel=(
                AnnotationPanel.from_dict(payload["panel"])
                if isinstance(payload.get("panel"), Mapping)
                else None
            ),
            leader=(
                AnnotationLeader.from_dict(payload["leader"])
                if isinstance(payload.get("leader"), Mapping)
                else None
            ),
            anchor=dict(payload.get("anchor") or {}),
            source_pts_range=SourcePtsRange.from_dict(payload["source_pts_range"]),
            screen_offset=tuple(payload.get("screen_offset", (0.0, 0.0))),
            style=AnnotationStyle.from_dict(payload.get("style")),
            visibility_policy=VisibilityPolicy.from_dict(
                payload.get("visibility_policy")
            ),
            user_visible=bool(payload.get("user_visible", True)),
        )
        return ApiResponse(
            201,
            {
                "annotation": result.annotation.to_dict(),
                "annotations_revision": result.manifest_revision,
                "render_revision": result.render_revision,
                "operation_id": result.operation_id,
            },
        )

    def _update_annotation(
        self,
        project_id: str,
        annotation_id: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        changes = payload.get("changes")
        if not isinstance(changes, Mapping):
            raise TypeError("annotation changes must be an object")
        result = self.service.annotation_service.update(
            project_id,
            annotation_id,
            expected_revision=int(payload["expected_revision"]),
            expected_annotation_revision=int(payload["expected_annotation_revision"]),
            changes=changes,
        )
        return ApiResponse(
            200,
            {
                "annotation": result.annotation.to_dict(),
                "annotations_revision": result.manifest_revision,
                "render_revision": result.render_revision,
                "operation_id": result.operation_id,
            },
        )

    def _delete_annotation(
        self,
        project_id: str,
        annotation_id: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        result = self.service.annotation_service.delete(
            project_id,
            annotation_id,
            expected_revision=int(payload["expected_revision"]),
            expected_annotation_revision=int(payload["expected_annotation_revision"]),
        )
        return ApiResponse(
            200,
            {
                "annotation_id": result.annotation_id,
                "annotations_revision": result.manifest_revision,
                "render_revision": result.render_revision,
                "operation_id": result.operation_id,
            },
        )

    def _track_annotation(
        self,
        project_id: str,
        annotation_id: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        from cadscene.annotations.tracking import VideoTrackingInitialization

        correction_payload = payload.get("correction")
        if correction_payload is not None and not isinstance(
            correction_payload, Mapping
        ):
            raise TypeError("tracking correction must be an object")
        correction = (
            None
            if correction_payload is None
            else VideoTrackingInitialization.from_dict(correction_payload)
        )
        result = self.service.track_video_annotation(
            project_id,
            annotation_id,
            expected_revision=int(payload["expected_revision"]),
            expected_annotation_revision=int(payload["expected_annotation_revision"]),
            correction=correction,
        )
        visible = sum(1 for item in result.revision.results if item.visible)
        lost = len(result.revision.results) - visible
        return ApiResponse(
            201,
            {
                "tracking_revision": result.revision.tracking_revision,
                "visible_frame_count": visible,
                "lost_frame_count": lost,
                "annotations_revision": result.annotation_result.manifest_revision,
                "annotation_revision": result.annotation_result.annotation.annotation_revision,
                "render_revision": result.annotation_result.render_revision,
                "operation_id": result.annotation_result.operation_id,
            },
        )

    def _annotation_tracking(self, project_id: str, annotation_id: str) -> ApiResponse:
        annotations = self.repositories.annotations.load(project_id)
        annotation = next(
            (
                item
                for item in annotations.annotations
                if item.annotation_id == annotation_id
            ),
            None,
        )
        if annotation is None:
            raise FileNotFoundError(f"annotation not found: {annotation_id}")
        if annotation.active_tracking_revision is None:
            raise FileNotFoundError("annotation has no active tracking revision")
        revision = self.service.tracking_revision_repository.load(
            project_id,
            annotation.clip_id,
            annotation.annotation_id,
            annotation.active_tracking_revision,
        )
        return ApiResponse(200, revision.to_dict(), {"Cache-Control": "no-store"})

    def _build_snapshot(self, project_id: str) -> dict[str, object]:
        with ExitStack() as stack:
            for repository in self.repositories.in_lock_order():
                stack.enter_context(repository.lock_for(project_id))
            return self._build_snapshot_locked(project_id)

    def _build_snapshot_locked(self, project_id: str) -> dict[str, object]:
        project = self.repositories.project.load(project_id)
        clips = self.repositories.clips.load(project_id)
        jobs = self.repositories.jobs.load(project_id)
        render = self.repositories.render.load(project_id)
        annotations = self.repositories.annotations.load(project_id)
        analysis_state = project.source_assets.get("_analysis")
        analysis_status = (
            str(analysis_state.get("status"))
            if isinstance(analysis_state, Mapping)
            else None
        )
        analysis_busy = analysis_status in {
            "queued",
            "preparing",
            "running",
            "validating",
        }
        analysis_job_ids = (
            tuple(str(item) for item in analysis_state.get("job_ids", ()))
            if isinstance(analysis_state, Mapping)
            else ()
        )
        analysis_jobs_by_id = {str(item.get("job_id")): item for item in jobs.jobs}
        analysis_jobs = [
            {
                "job_id": job_id,
                "job_type": candidate.get("job_type"),
                "status": candidate.get("status"),
                "stage": candidate.get("stage"),
                "progress": candidate.get("progress"),
                "depends_on_job_ids": list(candidate.get("depends_on_job_ids", ())),
                "error": candidate.get("error"),
            }
            for job_id in analysis_job_ids
            if (candidate := analysis_jobs_by_id.get(job_id)) is not None
        ]
        replacement_state = project.source_assets.get("_cad_replacement")
        replacement_job = None
        if isinstance(replacement_state, Mapping):
            replacement_job = analysis_jobs_by_id.get(
                str(replacement_state.get("job_id") or "")
            )
        replacement_eligibility = self.service.cad_replacement_eligibility(
            project_id
        )
        cad_replacement = {
            **replacement_eligibility,
            "status": (
                replacement_job.get("status")
                if isinstance(replacement_job, Mapping)
                else (
                    replacement_state.get("status")
                    if isinstance(replacement_state, Mapping)
                    else "idle"
                )
            ),
            "job_id": (
                replacement_state.get("job_id")
                if isinstance(replacement_state, Mapping)
                else None
            ),
            "progress": (
                _visible_job_progress(replacement_job)
                if isinstance(replacement_job, Mapping)
                else (
                    replacement_state.get("progress")
                    if isinstance(replacement_state, Mapping)
                    else None
                )
            ),
            "error": (
                replacement_job.get("error")
                if isinstance(replacement_job, Mapping)
                else (
                    replacement_state.get("error")
                    if isinstance(replacement_state, Mapping)
                    else None
                )
            ),
            "active_revision": (
                project.source_assets.get("cad", {}).get("revision")
                if isinstance(project.source_assets.get("cad"), Mapping)
                else None
            ),
            "version_count": len(project.source_assets.get("_cad_versions", ())),
        }
        preflight = self.service.preflight_trajectory_jobs(
            project_id, clip_ids=[clip.clip_id for clip in clips.clips]
        )
        render_preflight = self.service.preflight_render_jobs(
            project_id, clip_ids=[clip.clip_id for clip in clips.clips]
        )
        components = {
            "project": project.revision,
            "clips": clips.revision,
            "jobs": jobs.revision,
            "render": render.revision,
            "annotations": annotations.revision,
        }
        job_by_clip: dict[str, Mapping[str, object]] = {}
        render_job_by_clip: dict[str, Mapping[str, object]] = {}
        export_job_by_clip: dict[str, Mapping[str, object]] = {}
        scene_bridge_job_by_clip: dict[str, Mapping[str, object]] = {}
        for job in jobs.jobs:
            clip_id = job.get("clip_id")
            if isinstance(clip_id, str) and job.get("job_type") == "trajectory":
                job_by_clip[clip_id] = job
            elif isinstance(clip_id, str) and job.get("job_type") == "clip_render":
                render_job_by_clip[clip_id] = job
            elif isinstance(clip_id, str) and job.get("job_type") == "clip_export":
                export_job_by_clip[clip_id] = job
            elif isinstance(clip_id, str) and job.get("job_type") == "scene_bridge":
                scene_bridge_job_by_clip[clip_id] = job
        clip_payloads: list[dict[str, object]] = []
        can_start_any = False
        can_render_any = False
        for clip in clips.clips:
            trajectory_job = job_by_clip.get(clip.clip_id)
            render_job = render_job_by_clip.get(clip.clip_id)
            export_job = export_job_by_clip.get(clip.clip_id)
            scene_bridge_job = scene_bridge_job_by_clip.get(clip.clip_id)
            job = render_job or scene_bridge_job or trajectory_job
            if (
                trajectory_job is not None
                and trajectory_job.get("status")
                in {"queued", "preparing", "running", "validating"}
            ):
                job = trajectory_job
            elif (
                trajectory_job is not None
                and trajectory_job.get("status") == "success"
                and render_job is not None
                and render_job.get("status") in {"stale_input", "superseded"}
            ):
                job = trajectory_job
            elif (
                trajectory_job is not None
                and trajectory_job.get("status") == "success"
                and render_job is None
                and scene_bridge_job is not None
                and scene_bridge_job.get("status")
                in {"stale_input", "superseded"}
            ):
                job = trajectory_job
            display_job = job
            if job is not None and job.get("status") == "queued":
                display_job = next(
                    (
                        dependency
                        for dependency_id in job.get("depends_on_job_ids", ())
                        if (dependency := analysis_jobs_by_id.get(str(dependency_id)))
                        is not None
                        and dependency.get("status")
                        in {"preparing", "running", "validating"}
                    ),
                    job,
                )
            display_progress = (
                _pure_rotation_trajectory_progress(trajectory_job, display_job)
                if (
                    job is trajectory_job
                    and isinstance(trajectory_job, Mapping)
                    and trajectory_job.get("adapter_name") == "pure_rotation"
                )
                else _visible_job_progress(display_job)
            )
            capability = self._clip_capability(
                project_id,
                clip,
                preflight,
                render_preflight,
                job,
                analysis_busy=analysis_busy,
            )
            can_start_any = can_start_any or bool(
                capability["can_start_trajectory"]
                or capability["trajectory_needs_confirmation"]
            )
            can_render_any = can_render_any or bool(capability["can_render"])
            bridge_context = _scene_bridge_job_context(
                self.service, clip, scene_bridge_job
            )
            workbench_snapshot = (
                {"state": "unavailable", "workbench_output_revision": None}
                if self.workbench is None
                else self.workbench.snapshot_for_clip(project_id, clip)
            )
            bridge_reference = _scene_bridge_reference(clip)
            clip_payloads.append(
                {
                    "clip_id": clip.clip_id,
                    "job_id": None if job is None else job.get("job_id"),
                    "job_type": None if job is None else job.get("job_type"),
                    "display_name": clip.display_name,
                    "thumbnail_url": (
                        f"/api/projects/{project_id}/thumbnails/clips/{clip.clip_id}"
                    ),
                    "generated_display_name": clip.generated_display_name,
                    "custom_display_name": clip.custom_display_name,
                    "time_range": _friendly_range(clip),
                    "duration": _friendly_duration(clip),
                    "detected_motion_mode": clip.analysis.get(
                        "detected_motion_mode", "unknown"
                    ),
                    "confidence": clip.analysis.get("confidence"),
                    "recommended_workflow": clip.recommended_workflow,
                    "workflow_override": clip.workflow_override,
                    "resolved_workflow": clip.resolved_workflow,
                    "needs_review": bool(clip.analysis.get("needs_review", False)),
                    "status": (
                        "ready" if display_job is None else display_job.get("status")
                    ),
                    "stage": (
                        None if display_job is None else display_job.get("stage")
                    ),
                    "progress": display_progress,
                    "render": {
                        "job_id": None
                        if render_job is None
                        else render_job.get("job_id"),
                        "status": "not_started"
                        if render_job is None
                        else render_job.get("status"),
                        "stage": None
                        if render_job is None
                        else render_job.get("stage"),
                        "progress": _visible_job_progress(render_job),
                        "output_revision": None
                        if render_job is None
                        else render_job.get("output_revision"),
                        "preview_url": _render_preview_url(
                            project_id,
                            clip.clip_id,
                            render_job,
                            render.clip_renders,
                        ),
                        "preview_is_current": _render_preview_is_current(
                            clip.clip_id, render_job, render.clip_renders
                        ),
                    },
                    "scene_bridge": {
                        "job_id": None
                        if scene_bridge_job is None
                        else scene_bridge_job.get("job_id"),
                        "status": "not_started"
                        if scene_bridge_job is None
                        else scene_bridge_job.get("status"),
                        "stage": None
                        if scene_bridge_job is None
                        else scene_bridge_job.get("stage"),
                        "progress": _visible_job_progress(scene_bridge_job),
                        "error": None
                        if scene_bridge_job is None
                        else scene_bridge_job.get("error"),
                        **bridge_context,
                    },
                    "capabilities": capability,
                    "workbench": {
                        **workbench_snapshot,
                        "bridge_revision": None
                        if bridge_reference is None
                        else bridge_reference.value.get("bridge_revision"),
                        "preparation": (
                            None
                            if export_job is None
                            else {
                                "job_id": export_job.get("job_id"),
                                "status": export_job.get("status"),
                                "stage": export_job.get("stage"),
                                "progress": export_job.get("progress"),
                                "error": export_job.get("error"),
                            }
                        ),
                    },
                    "source_interval": {
                        "start_pts": clip.analysis.get("source_start_pts"),
                        "end_pts_exclusive": clip.analysis.get(
                            "source_end_pts_exclusive"
                        ),
                        "time_base": clip.analysis.get("source_time_base"),
                        "semantics": "half_open",
                    },
                }
            )
        can_merge = bool(clips.clips) and all(
            _render_preview_is_current(
                clip.clip_id,
                render_job_by_clip.get(clip.clip_id),
                render.clip_renders,
            )
            for clip in clips.clips
        )
        merge_job = next(
            (
                item
                for item in reversed(jobs.jobs)
                if item.get("job_type") == "project_merge"
            ),
            None,
        )
        merge_download_url = None
        if merge_job is not None and merge_job.get("status") == "success":
            try:
                self.service.published_merge_video_path(project_id)
                merge_download_url = f"/api/projects/{project_id}/merge-output/video"
            except (FileNotFoundError, OSError, ValueError, TypeError):
                pass
        merge_status = (
            "not_started"
            if merge_job is None
            else (
                "stale_input"
                if merge_job.get("status") == "success"
                and merge_download_url is None
                else merge_job.get("status")
            )
        )
        snapshot = {
            "project_id": project_id,
            "display_name": str(
                project.source_assets.get("display_name") or project_id
            ),
            "component_revisions": components,
            "project_state": project.project_state,
            "active_analysis_revision": project.active_analysis_revision,
            "candidate_analysis_revision": project.candidate_analysis_revision,
            "candidate_analysis_preview": _candidate_analysis_preview(project),
            "analysis": {
                "status": analysis_status,
                "jobs": analysis_jobs,
            },
            "cad_replacement": cad_replacement,
            "assets": _snapshot_assets(project_id, project.source_assets),
            "capabilities": {
                "can_start_trajectory": can_start_any,
                "can_render": can_render_any,
                "can_merge": can_merge,
                "can_reanalyze": _analysis_request_key(project.source_assets)
                is not None
                and not analysis_busy,
            },
            "merge": {
                "job_id": None if merge_job is None else merge_job.get("job_id"),
                "status": merge_status,
                "stage": None if merge_job is None else merge_job.get("stage"),
                "progress": _visible_job_progress(merge_job),
                "download_url": merge_download_url,
            },
            "clips": clip_payloads,
            "annotations": [
                annotation.to_dict() for annotation in annotations.annotations
            ],
        }
        revision_payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            **snapshot,
            "snapshot_revision": sha256(revision_payload).hexdigest(),
        }

    def _clip_capability(
        self,
        project_id: str,
        clip: ClipDefinition,
        preflight: TrajectoryPreflight,
        render_preflight: RenderPreflight,
        job: Mapping[str, object] | None,
        *,
        analysis_busy: bool = False,
    ) -> dict[str, object]:
        can_start = not analysis_busy and clip.clip_id in preflight.eligible
        needs_confirmation = (
            not analysis_busy and clip.clip_id in preflight.needs_confirmation
        )
        reason = preflight.reasons.get(clip.clip_id)
        if analysis_busy:
            reason = "project analysis is still running"
        status = None if job is None else str(job.get("status"))
        can_open_workbench = (
            False
            if self.workbench is None
            else self.workbench.can_open(project_id, clip.clip_id)
        )
        can_prepare_workbench = (
            False
            if self.workbench is None
            else self.workbench.can_prepare(project_id, clip.clip_id)
        )
        location = (
            {
                "can_locate_up": False,
                "can_locate_down": False,
                "locate_up_target_clip_id": None,
                "locate_down_target_clip_id": None,
            }
            if self.workbench is None
            else self.workbench.adjacent_location_capabilities(
                project_id, clip.clip_id
            )
        )
        bridge = {
            "can_bridge_up": bool(location.get("can_locate_up")),
            "can_bridge_down": bool(location.get("can_locate_down")),
            "bridge_up_reason": location.get("locate_up_reason"),
            "bridge_down_reason": location.get("locate_down_reason"),
            "bridge_up_target_clip_id": location.get("locate_up_target_clip_id"),
            "bridge_down_target_clip_id": location.get(
                "locate_down_target_clip_id"
            ),
        }
        return {
            "can_start_trajectory": can_start,
            "trajectory_needs_confirmation": needs_confirmation,
            "reason": reason,
            "can_open_workbench": can_open_workbench,
            "can_prepare_workbench": can_prepare_workbench,
            "can_render": not analysis_busy
            and (
                clip.clip_id in render_preflight.eligible
                or clip.clip_id in render_preflight.confirmation_required
            ),
            "render_needs_confirmation": (
                not analysis_busy
                and clip.clip_id in render_preflight.confirmation_required
            ),
            "render_reason": (
                "project analysis is still running"
                if analysis_busy
                else render_preflight.reasons.get(clip.clip_id)
            ),
            "can_retry": status
            in {"failed", "interrupted", "cancelled", "stale_input", "superseded"},
            "can_cancel": status in {"queued", "preparing", "running", "validating"},
            **location,
            **bridge,
        }

    def _locate_adjacent_clip(
        self, project_id: str, source_clip_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        direction = payload.get("direction")
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        return_to = payload.get("return_to")
        if not isinstance(return_to, str):
            raise InvalidWorkbenchReturnPath("return_to is required")
        expected_jobs_revision = payload.get("expected_jobs_revision")
        if not isinstance(expected_jobs_revision, int) or isinstance(
            expected_jobs_revision, bool
        ):
            raise ValueError("expected_jobs_revision is required")
        session, target_clip_id = self.workbench.locate_adjacent(
            project_id,
            source_clip_id,
            direction=str(direction),
            return_to=return_to,
            expected_clips_revision=_required_revision(payload),
        )
        if session is None:
            job = self.service.enqueue_workbench_clip_export(
                project_id,
                target_clip_id,
                expected_jobs_revision=expected_jobs_revision,
            )
            return ApiResponse(
                202,
                {
                    "state": "preparing_clip",
                    "message": "正在准备相邻片段，完成后将自动应用定位",
                    "job_id": job.job_id,
                    "source_clip_id": source_clip_id,
                    "target_clip_id": target_clip_id,
                    "direction": direction,
                    "clips_revision": self.repositories.clips.load(
                        project_id
                    ).revision,
                    "jobs_revision": self.repositories.jobs.load(project_id).revision,
                },
            )
        return ApiResponse(
            201,
            {
                **self.workbench.session_payload(session),
                "source_clip_id": source_clip_id,
                "target_clip_id": target_clip_id,
                "direction": direction,
                "workbench_url": self.workbench.workbench_url(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
            },
        )

    def _enqueue_scene_bridge(
        self, project_id: str, source_clip_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        direction = payload.get("direction")
        if direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        expected_jobs_revision = payload.get("expected_jobs_revision")
        if not isinstance(expected_jobs_revision, int) or isinstance(
            expected_jobs_revision, bool
        ):
            raise ValueError("expected_jobs_revision is required")
        result = self.service.enqueue_scene_bridge(
            project_id,
            source_clip_id,
            direction=str(direction),
            expected_jobs_revision=expected_jobs_revision,
            expected_clips_revision=_required_revision(payload),
        )
        return ApiResponse(
            202,
            {
                "state": "scene_bridge_queued",
                "message": "正在使用重叠帧打通相邻片段路线",
                "job_id": result.job.job_id,
                "source_clip_id": result.source_clip_id,
                "target_clip_id": result.target_clip_id,
                "direction": result.direction,
                "clips_revision": self.repositories.clips.load(project_id).revision,
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
            },
        )

    def _create_workbench_session(
        self, project_id: str, clip_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        return_to = payload.get("return_to")
        if not isinstance(return_to, str):
            raise InvalidWorkbenchReturnPath("return_to is required")
        context = self.workbench.resolve_context(project_id, clip_id)
        if not context.can_open_workbench:
            if not self.workbench.can_prepare(project_id, clip_id):
                raise WorkbenchPermissionDenied("片段视频或项目 CAD 尚未准备完成")
            expected_jobs_revision = payload.get("expected_jobs_revision")
            if not isinstance(expected_jobs_revision, int) or isinstance(
                expected_jobs_revision, bool
            ):
                raise ValueError("expected_jobs_revision is required")
            job = self.service.enqueue_workbench_clip_export(
                project_id,
                clip_id,
                expected_jobs_revision=expected_jobs_revision,
            )
            return ApiResponse(
                202,
                {
                    "state": "preparing_clip",
                    "message": "正在按原视频时间范围准备片段视频",
                    "job_id": job.job_id,
                    "jobs_revision": self.repositories.jobs.load(project_id).revision,
                },
            )
        session = self.workbench.open(
            project_id,
            clip_id,
            return_to=return_to,
            expected_clips_revision=_required_revision(payload),
        )
        return ApiResponse(
            201,
            {
                **self.workbench.session_payload(session),
                "workbench_url": self.workbench.workbench_url(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
            },
        )

    def _inspect_workbench_session(self, project_id: str, token: str) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        session = self.workbench.inspect(project_id, token)
        return ApiResponse(
            200,
            {
                **self.workbench.session_payload(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
            },
        )

    def _attach_workbench_trajectory(
        self, project_id: str, token: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        session = self.workbench.attach_trajectory(
            project_id,
            token,
            expected_clips_revision=_required_revision(payload),
        )
        return ApiResponse(
            200,
            {
                **self.workbench.session_payload(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
            },
        )

    def _heartbeat_workbench_session(
        self, project_id: str, token: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        session = self.workbench.heartbeat(
            project_id,
            token,
            expected_clips_revision=_required_revision(payload),
        )
        return ApiResponse(
            200,
            {
                **self.workbench.session_payload(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
            },
        )

    def _update_workbench_resume(
        self, project_id: str, token: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        expected = payload.get("expected_resume_revision")
        if expected is not None and (
            not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
        ):
            raise ValueError("expected_resume_revision must be null or non-negative")
        operation_id = payload.get("operation_id")
        workflow_stage = payload.get("workflow_stage")
        source_pts = payload.get("source_pts")
        source_time_base = payload.get("source_time_base")
        if not isinstance(operation_id, str):
            raise ValueError("operation_id is required")
        if not isinstance(workflow_stage, str):
            raise ValueError("workflow_stage is required")
        if not isinstance(source_pts, int) or isinstance(source_pts, bool):
            raise ValueError("source_pts must be an integer")
        if not isinstance(source_time_base, Mapping):
            raise ValueError("source_time_base is required")
        state = self.workbench.update_resume(
            project_id,
            token,
            expected_resume_revision=expected,
            operation_id=operation_id,
            workflow_stage=workflow_stage,
            source_pts=source_pts,
            source_time_base=source_time_base,
            quality_revision=_optional_revision(payload.get("quality_revision")),
            render_revision=_optional_revision(payload.get("render_revision")),
        )
        session = self.workbench.inspect(project_id, token)
        return ApiResponse(
            200,
            {
                **self.workbench.session_payload(session),
                "resume_state": state.to_dict(),
            },
        )

    def _mutate_workbench_session(
        self,
        project_id: str,
        token: str,
        action: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied(
                "project workbench sessions are unavailable"
            )
        expected_revision = _required_revision(payload)
        if action == "save":
            existing_save = payload.get("existing_save")
            if not isinstance(existing_save, Mapping):
                raise InvalidWorkbenchOutput("existing_save is required")
            session = self.workbench.save(
                project_id,
                token,
                existing_save,
                expected_clips_revision=expected_revision,
            )
        else:
            session = self.workbench.close(
                project_id,
                token,
                expected_clips_revision=expected_revision,
            )
        return ApiResponse(
            200,
            {
                **self.workbench.session_payload(session),
                "clips_revision": self.repositories.clips.load(project_id).revision,
            },
        )

    def _update_workflow(
        self,
        project_id: str,
        clip_id: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        if "workflow_override" not in payload:
            raise ValueError("workflow_override is required and may be null")
        override = payload["workflow_override"]
        if override is not None and not isinstance(override, str):
            raise TypeError("workflow_override must be a string or null")
        manifest = self.service.update_clip_workflow(
            project_id,
            clip_id,
            expected_revision=_required_revision(payload),
            workflow_override=override,
        )
        clip = next(item for item in manifest.clips if item.clip_id == clip_id)
        return ApiResponse(
            200,
            {
                "clip_id": clip_id,
                "clips_revision": manifest.revision,
                "workflow_override": clip.workflow_override,
                "resolved_workflow": clip.resolved_workflow,
            },
        )

    def _update_name(
        self,
        project_id: str,
        clip_id: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        if "custom_display_name" not in payload:
            raise ValueError("custom_display_name is required and may be null")
        name = payload["custom_display_name"]
        if name is not None and not isinstance(name, str):
            raise TypeError("custom_display_name must be a string or null")
        manifest = self.service.update_clip_display_name(
            project_id,
            clip_id,
            expected_revision=_required_revision(payload),
            custom_display_name=name,
        )
        clip = next(item for item in manifest.clips if item.clip_id == clip_id)
        return ApiResponse(
            200,
            {
                "clip_id": clip_id,
                "clips_revision": manifest.revision,
                "custom_display_name": clip.custom_display_name,
                "display_name": clip.display_name,
            },
        )

    def _trajectory_jobs(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        expected_revision = _required_revision(payload)
        current = self.repositories.jobs.load(project_id)
        if current.revision != expected_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected_revision,
                current_revision=current.revision,
            )
        clip_ids = _string_sequence(payload.get("clip_ids"), "clip_ids")
        preflight = self.service.preflight_trajectory_jobs(
            project_id, clip_ids=clip_ids or None
        )
        if not bool(payload.get("enqueue", False)):
            return ApiResponse(200, _preflight_payload(preflight))
        confirmed = _string_sequence(
            payload.get("confirmed_clip_ids"), "confirmed_clip_ids"
        )
        result = self.service.enqueue_trajectory_jobs(
            project_id,
            clip_ids=clip_ids or None,
            confirmed_clip_ids=confirmed,
            expected_jobs_revision=expected_revision,
        )
        return ApiResponse(
            202,
            {
                **_preflight_payload(result.preflight),
                "enqueued_clip_ids": list(result.enqueued_clip_ids),
                "job_ids": list(result.job_ids),
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
            },
        )

    def _render_jobs(
        self, project_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        expected_revision = _required_revision(payload)
        current = self.repositories.jobs.load(project_id)
        if current.revision != expected_revision:
            raise RevisionConflict(
                project_id=project_id,
                expected_revision=expected_revision,
                current_revision=current.revision,
            )
        clip_ids = _string_sequence(payload.get("clip_ids"), "clip_ids")
        preflight = self.service.preflight_render_jobs(
            project_id, clip_ids=clip_ids or None
        )
        if not bool(payload.get("enqueue", False)):
            return ApiResponse(200, _render_preflight_payload(preflight))
        confirmed = _string_sequence(
            payload.get("confirmed_clip_ids"), "confirmed_clip_ids"
        )
        result = self.service.enqueue_render_jobs(
            project_id,
            clip_ids=clip_ids or None,
            confirmed_clip_ids=confirmed,
            expected_jobs_revision=expected_revision,
        )
        return ApiResponse(
            202,
            {
                **_render_preflight_payload(result.preflight),
                "enqueued_clip_ids": list(result.enqueued_clip_ids),
                "job_ids": list(result.job_ids),
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
            },
        )

    def _merge_job(self, project_id: str, payload: Mapping[str, object]) -> ApiResponse:
        expected_revision = _required_revision(payload)
        result = self.service.enqueue_project_merge(
            project_id, expected_jobs_revision=expected_revision
        )
        return ApiResponse(
            202,
            {
                "job_id": result.job_id,
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
                "preflight": result.preflight.to_dict(),
            },
        )

    def _job_action(
        self,
        project_id: str,
        job_id: str,
        action: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        expected_revision = _required_revision(payload)
        job = (
            self.service.cancel_job(
                project_id,
                job_id,
                expected_jobs_revision=expected_revision,
            )
            if action == "cancel"
            else self.service.retry_job(
                project_id,
                job_id,
                expected_jobs_revision=expected_revision,
            )
        )
        return ApiResponse(
            202,
            {
                "project_id": project_id,
                "job_id": job.job_id,
                "status": job.status,
                "jobs_revision": self.repositories.jobs.load(project_id).revision,
            },
        )


def _required_revision(payload: Mapping[str, object]) -> int:
    value = payload.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    return value


def _string_sequence(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not is_safe_stable_id(item) for item in value
    ):
        raise ValueError(f"{name} must be a list of stable IDs")
    return tuple(value)


def _preflight_payload(preflight: TrajectoryPreflight) -> dict[str, object]:
    return {
        "eligible": list(preflight.eligible),
        "needs_confirmation": list(preflight.needs_confirmation),
        "skipped": list(preflight.skipped),
        "reasons": dict(preflight.reasons),
    }


def _render_preflight_payload(preflight: RenderPreflight) -> dict[str, object]:
    return {
        "eligible": list(preflight.eligible),
        "confirmation_required": list(preflight.confirmation_required),
        "skipped": list(preflight.skipped),
        "reasons": dict(preflight.reasons),
    }


def _visible_job_progress(
    job: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if job is None:
        return None
    status = str(job.get("status"))
    if job.get("job_type") == "clip_render" and status in {
        "queued",
        "preparing",
        "running",
        "validating",
    }:
        stored = job.get("progress")
        progress = (
            dict(stored)
            if isinstance(stored, Mapping)
            else {
                "stage": job.get("stage") or status,
                "message": "render job is queued" if status == "queued" else status,
            }
        )
        fraction = progress.get("fraction")
        if status in {"queued", "preparing"}:
            progress["fraction"] = 0.0
        elif status == "validating":
            progress["fraction"] = 0.99
        elif isinstance(fraction, (int, float)) and not isinstance(fraction, bool):
            progress["fraction"] = min(float(fraction), 0.99)
        else:
            progress["fraction"] = 0.0
        return progress
    if not isinstance(job.get("progress"), Mapping):
        return None
    progress = dict(job["progress"])
    fraction = progress.get("fraction")
    if (
        status in {"queued", "preparing", "running", "validating"}
        and isinstance(fraction, (int, float))
        and not isinstance(fraction, bool)
    ):
        progress["fraction"] = min(float(fraction), 0.99)
    return progress


def _pure_rotation_trajectory_progress(
    trajectory_job: Mapping[str, object],
    display_job: Mapping[str, object] | None,
) -> dict[str, object]:
    visible = _visible_job_progress(display_job)
    if visible is None:
        visible = {
            "stage": (
                "queued" if display_job is None else display_job.get("stage", "queued")
            ),
            "message": (
                "等待片段准备"
                if display_job is None
                else display_job.get("stage", "正在准备片段")
            ),
        }
    trajectory_id = str(trajectory_job.get("job_id") or "")
    display_id = "" if display_job is None else str(display_job.get("job_id") or "")
    raw_fraction = visible.get("fraction")
    measured = (
        float(raw_fraction)
        if isinstance(raw_fraction, (int, float)) and not isinstance(raw_fraction, bool)
        else 0.0
    )
    if display_id != trajectory_id:
        overall = 0.15 * min(1.0, max(0.0, measured))
    elif trajectory_job.get("status") == "success":
        overall = 1.0
    else:
        overall = min(0.99, 0.15 + 0.85 * min(1.0, max(0.0, measured)))
    return {**visible, "fraction": overall}


def _clip_seconds(clip: ClipDefinition) -> tuple[float, float]:
    time_base = clip.analysis.get("source_time_base")
    if not isinstance(time_base, Mapping):
        raise ValueError(f"clip {clip.clip_id} is missing source_time_base")
    fraction = Fraction(int(time_base["numerator"]), int(time_base["denominator"]))
    start = Fraction(int(clip.analysis["source_start_pts"])) * fraction
    end = Fraction(int(clip.analysis["source_end_pts_exclusive"])) * fraction
    return float(start), float(end)


def _clock(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _friendly_range(clip: ClipDefinition) -> str:
    start, end = _clip_seconds(clip)
    return f"{_clock(start)} – {_clock(end)}"


def _friendly_duration(clip: ClipDefinition) -> str:
    start, end = _clip_seconds(clip)
    return _clock(end - start)


def _candidate_analysis_preview(
    project: ProjectManifest,
) -> dict[str, object] | None:
    revision = project.candidate_analysis_revision
    if revision is None:
        return None
    descriptors = project.source_assets.get("_analysis_revisions")
    if not isinstance(descriptors, Mapping):
        return None
    descriptor = descriptors.get(revision)
    if not isinstance(descriptor, Mapping):
        return None
    input_snapshot = descriptor.get("input_snapshot")
    artifact_path = descriptor.get("analysis_artifact_path")
    if not isinstance(input_snapshot, Mapping) or not isinstance(artifact_path, str):
        return None
    manifest_path = Path(artifact_path) / "02_video_analysis" / "clip_manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("analysis_revision") != revision:
            return None
        clips = tuple(
            ClipDefinition.from_analysis(
                {**item, "input_snapshot": dict(input_snapshot)},
                generated_display_name=(
                    f"场景 {int(item.get('scene_index', 1)):02d} · "
                    f"第 {int(item.get('segment_index', 1))} 段"
                ),
            )
            for item in payload.get("clips", ())
            if isinstance(item, Mapping)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return {
        "clip_count": len(clips),
        "clips": [
            {
                "display_name": clip.generated_display_name,
                "time_range": _friendly_range(clip),
                "duration": _friendly_duration(clip),
                "detected_motion_mode": clip.analysis.get(
                    "detected_motion_mode", "unknown"
                ),
                "confidence": clip.analysis.get("confidence"),
                "recommended_workflow": clip.recommended_workflow,
                "needs_review": bool(clip.analysis.get("needs_review", False)),
            }
            for clip in clips
        ],
    }


def _render_preview_url(
    project_id: str,
    clip_id: str,
    render_job: Mapping[str, object] | None,
    render_records: tuple[Mapping[str, object], ...],
) -> str | None:
    record = _render_preview_record(clip_id, render_job, render_records)
    if record is None:
        return None
    output_revision = record["output_revision"]
    return (
        f"/api/projects/{project_id}/clips/{clip_id}/renders/"
        f"{output_revision}/video"
    )


def _scene_bridge_reference(clip: ClipDefinition) -> StateReference | None:
    return next(
        (
            reference
            for reference in reversed(clip.references)
            if reference.owner == "jobs"
            and reference.value.get("reference_type") == "scene_bridge"
            and reference.value.get("target_clip_id") == clip.clip_id
        ),
        None,
    )


def _scene_bridge_job_context(
    service: ProjectService,
    clip: ClipDefinition,
    job: Mapping[str, object] | None,
) -> dict[str, object]:
    reference = _scene_bridge_reference(clip)
    if reference is not None:
        return {
            "source_clip_id": reference.value.get("source_clip_id"),
            "target_clip_id": clip.clip_id,
            "direction": reference.value.get("direction"),
            "bridge_revision": reference.value.get("bridge_revision"),
        }
    if not isinstance(job, Mapping):
        return {
            "source_clip_id": None,
            "target_clip_id": clip.clip_id,
            "direction": None,
            "bridge_revision": None,
        }
    operation_id = job.get("operation_id")
    project_id = job.get("project_id")
    if not isinstance(operation_id, str) or not isinstance(project_id, str):
        return {}
    path = (
        service.projects_root
        / project_id
        / "scene_bridge_requests"
        / f"{operation_id}.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        identity = payload.get("runner_identity")
    except (OSError, json.JSONDecodeError):
        identity = None
    if not isinstance(identity, Mapping):
        return {}
    return {
        "source_clip_id": identity.get("source_clip_id"),
        "target_clip_id": identity.get("target_clip_id"),
        "direction": identity.get("direction"),
        "bridge_revision": None,
    }


def _render_preview_is_current(
    clip_id: str,
    render_job: Mapping[str, object] | None,
    render_records: tuple[Mapping[str, object], ...],
) -> bool:
    record = _render_preview_record(clip_id, render_job, render_records)
    return bool(
        record is not None
        and isinstance(render_job, Mapping)
        and render_job.get("status") == "success"
        and render_job.get("output_revision") == record.get("output_revision")
        and record.get("status") == "success"
    )


def _render_preview_record(
    clip_id: str,
    render_job: Mapping[str, object] | None,
    render_records: tuple[Mapping[str, object], ...],
) -> Mapping[str, object] | None:
    output_revision = (
        render_job.get("output_revision") if isinstance(render_job, Mapping) else None
    )
    return next(
        (
            record
            for record in reversed(render_records)
            if record.get("clip_id") == clip_id
            and (
                not is_safe_stable_id(output_revision)
                or record.get("output_revision") == output_revision
            )
            and is_safe_stable_id(record.get("output_revision"))
            and record.get("status") in {"success", "stale_input"}
        ),
        None,
    )


def _snapshot_assets(
    project_id: str, source_assets: Mapping[str, object]
) -> dict[str, object]:
    assets: dict[str, object] = dict(source_assets)
    video = assets.get("video")
    if not isinstance(video, Mapping) and assets.get("video_path"):
        video = {
            "path": str(assets["video_path"]),
            "original_filename": Path(str(assets["video_path"])).name,
        }
    if isinstance(video, Mapping):
        assets["video"] = {
            **video,
            "thumbnail_url": f"/api/projects/{project_id}/thumbnails/source",
        }
    return assets


def _analysis_request_key(source_assets: Mapping[str, object]) -> str | None:
    fingerprints: dict[str, str] = {}
    for required in ("video", "cad"):
        asset = source_assets.get(required)
        if (
            not isinstance(asset, Mapping)
            or not asset.get("path")
            or not asset.get("sha256")
        ):
            return None
        fingerprints[required] = str(asset["sha256"])
    srt = source_assets.get("srt")
    if isinstance(srt, Mapping) and srt.get("sha256"):
        fingerprints["srt"] = str(srt["sha256"])
    payload = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("ascii")).hexdigest()


def _optional_revision(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("revision must be null or a non-empty trimmed string")
    return value
