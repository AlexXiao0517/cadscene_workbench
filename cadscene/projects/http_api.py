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
from .models import ClipDefinition
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
_ANALYSIS = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/analysis/start$")
_SNAPSHOT = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/snapshot$")
_WORKFLOW = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/workflow$"
)
_NAME = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/name$"
)
_TRAJECTORY = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/trajectory-jobs$"
)
_RENDER = re.compile(rf"^/api/projects/(?P<project>{_SAFE_ID})/render-jobs$")
_JOB_ACTION = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/jobs/(?P<job>{_SAFE_ID})/(?P<action>retry|cancel)$"
)
_JOB_RUNTIME = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/jobs/(?P<job>{_SAFE_ID})/runtime$"
)
_WORKBENCH_CREATE = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/clips/(?P<clip>{_SAFE_ID})/workbench-sessions$"
)
_WORKBENCH_SESSION = re.compile(
    rf"^/api/projects/(?P<project>{_SAFE_ID})/workbench-sessions/(?P<token>[A-Za-z0-9_-]+)(?:/(?P<action>save|close|trajectory-ready))?$"
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
            if method == "POST" and path == "/api/projects":
                return self._create_project(payload)
            match = _SNAPSHOT.fullmatch(path)
            if method == "GET" and match:
                return self._snapshot(match["project"], headers)
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
            match = _WORKFLOW.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_workflow(
                    match["project"], match["clip"], payload
                )
            match = _NAME.fullmatch(path)
            if method == "PATCH" and match:
                return self._update_name(match["project"], match["clip"], payload)
            match = _TRAJECTORY.fullmatch(path)
            if method == "POST" and match:
                return self._trajectory_jobs(match["project"], payload)
            match = _RENDER.fullmatch(path)
            if method == "POST" and match:
                return self._render_jobs(match["project"], payload)
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
            match = _WORKBENCH_CREATE.fullmatch(path)
            if method == "POST" and match:
                return self._create_workbench_session(
                    match["project"], match["clip"], payload
                )
            match = _WORKBENCH_SESSION.fullmatch(path)
            if match and method == "GET" and match["action"] is None:
                return self._inspect_workbench_session(
                    match["project"], match["token"]
                )
            if match and method == "POST" and match["action"] in {"save", "close"}:
                return self._mutate_workbench_session(
                    match["project"], match["token"], match["action"], payload
                )
            if match and method == "POST" and match["action"] == "trajectory-ready":
                return self._attach_workbench_trajectory(
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
            return ApiResponse(409, {"error": "workbench_save_replayed", "message": str(exc)})
        except StaleWorkbenchSession as exc:
            return ApiResponse(409, {"error": "stale_workbench_session", "message": str(exc)})
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

    def _publish_upload(
        self,
        project_id: str,
        asset_type: str,
        payload: Mapping[str, object],
        request: UploadRequest,
    ) -> ApiResponse:
        return self._publish_upload_locked(
            project_id, asset_type, payload, request
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

    def _snapshot(
        self, project_id: str, headers: Mapping[str, str]
    ) -> ApiResponse:
        snapshot = self._build_snapshot(project_id)
        etag = f'"{snapshot["snapshot_revision"]}"'
        response_headers = {"ETag": etag, "Cache-Control": "no-store"}
        if headers.get("if-none-match") == etag:
            return ApiResponse(304, None, response_headers)
        return ApiResponse(200, snapshot, response_headers)

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
        analysis_jobs_by_id = {
            str(item.get("job_id")): item for item in jobs.jobs
        }
        analysis_jobs = [
            {
                "job_id": job_id,
                "job_type": candidate.get("job_type"),
                "status": candidate.get("status"),
                "stage": candidate.get("stage"),
                "progress": candidate.get("progress"),
                "depends_on_job_ids": list(
                    candidate.get("depends_on_job_ids", ())
                ),
                "error": candidate.get("error"),
            }
            for job_id in analysis_job_ids
            if (candidate := analysis_jobs_by_id.get(job_id)) is not None
        ]
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
        }
        job_by_clip: dict[str, Mapping[str, object]] = {}
        render_job_by_clip: dict[str, Mapping[str, object]] = {}
        export_job_by_clip: dict[str, Mapping[str, object]] = {}
        for job in jobs.jobs:
            clip_id = job.get("clip_id")
            if isinstance(clip_id, str) and job.get("job_type") == "trajectory":
                job_by_clip[clip_id] = job
            elif isinstance(clip_id, str) and job.get("job_type") == "clip_render":
                render_job_by_clip[clip_id] = job
            elif isinstance(clip_id, str) and job.get("job_type") == "clip_export":
                export_job_by_clip[clip_id] = job
        clip_payloads: list[dict[str, object]] = []
        can_start_any = False
        can_render_any = False
        for clip in clips.clips:
            trajectory_job = job_by_clip.get(clip.clip_id)
            render_job = render_job_by_clip.get(clip.clip_id)
            export_job = export_job_by_clip.get(clip.clip_id)
            job = render_job or trajectory_job
            capability = self._clip_capability(
                project_id, clip, preflight, render_preflight, job,
                analysis_busy=analysis_busy,
            )
            can_start_any = can_start_any or bool(
                capability["can_start_trajectory"]
                or capability["trajectory_needs_confirmation"]
            )
            can_render_any = can_render_any or bool(capability["can_render"])
            clip_payloads.append(
                {
                    "clip_id": clip.clip_id,
                    "job_id": None if job is None else job.get("job_id"),
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
                    "status": "ready" if job is None else job.get("status"),
                    "stage": None if job is None else job.get("stage"),
                    "progress": None if job is None else job.get("progress"),
                    "render": {
                        "job_id": None if render_job is None else render_job.get("job_id"),
                        "status": "not_started" if render_job is None else render_job.get("status"),
                        "stage": None if render_job is None else render_job.get("stage"),
                        "progress": None if render_job is None else render_job.get("progress"),
                        "output_revision": None if render_job is None else render_job.get("output_revision"),
                    },
                    "capabilities": capability,
                    "workbench": {
                        **(
                            {"state": "unavailable", "workbench_output_revision": None}
                            if self.workbench is None
                            else self.workbench.snapshot_for_clip(project_id, clip)
                        ),
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
        snapshot = {
            "project_id": project_id,
            "display_name": str(project.source_assets.get("display_name") or project_id),
            "component_revisions": components,
            "project_state": project.project_state,
            "active_analysis_revision": project.active_analysis_revision,
            "analysis": {
                "status": analysis_status,
                "jobs": analysis_jobs,
            },
            "assets": _snapshot_assets(project_id, project.source_assets),
            "capabilities": {
                "can_start_trajectory": can_start_any,
                "can_render": can_render_any,
                "can_merge": False,
                "can_reanalyze": _analysis_request_key(project.source_assets)
                is not None
                and not analysis_busy,
            },
            "clips": clip_payloads,
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
        return {
            "can_start_trajectory": can_start,
            "trajectory_needs_confirmation": needs_confirmation,
            "reason": reason,
            "can_open_workbench": can_open_workbench,
            "can_prepare_workbench": can_prepare_workbench,
            "can_render": not analysis_busy and (
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
            "can_cancel": status
            in {"queued", "preparing", "running", "validating"},
        }

    def _create_workbench_session(
        self, project_id: str, clip_id: str, payload: Mapping[str, object]
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied("project workbench sessions are unavailable")
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

    def _inspect_workbench_session(
        self, project_id: str, token: str
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied("project workbench sessions are unavailable")
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
            raise WorkbenchPermissionDenied("project workbench sessions are unavailable")
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

    def _mutate_workbench_session(
        self,
        project_id: str,
        token: str,
        action: str,
        payload: Mapping[str, object],
    ) -> ApiResponse:
        if self.workbench is None:
            raise WorkbenchPermissionDenied("project workbench sessions are unavailable")
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
        not is_safe_stable_id(item)
        for item in value
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
        if not isinstance(asset, Mapping) or not asset.get("path") or not asset.get("sha256"):
            return None
        fingerprints[required] = str(asset["sha256"])
    srt = source_assets.get("srt")
    if isinstance(srt, Mapping) and srt.get("sha256"):
        fingerprints["srt"] = str(srt["sha256"])
    payload = json.dumps(fingerprints, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("ascii")).hexdigest()
