from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from .adapters import AdapterResult, WorkflowAdapterRegistry
from .json_repositories import ProjectRepositories
from .models import ClipDefinition, JobsManifest
from .queue import AttemptRecord, LocalResourceQueue, QueueJob


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
        self.now = now
        self._identity = identity or (lambda: uuid4().hex)

    def preflight_trajectory_jobs(
        self,
        project_id: str,
        *,
        clip_ids: Sequence[str] | None = None,
    ) -> TrajectoryPreflight:
        project = self.repositories.project.load(project_id)
        clips_manifest = self.repositories.clips.load(project_id)
        selected = _select_clips(clips_manifest.clips, clip_ids)
        video_path = _asset_path(project.source_assets, "video")
        srt_path = _asset_path(project.source_assets, "srt")
        eligible: list[str] = []
        confirmation: list[str] = []
        skipped: list[str] = []
        reasons: dict[str, str] = {}
        for clip in selected:
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
                reasons[clip.clip_id] = adapter.unavailable_reason or "adapter unavailable"
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
                reasons[clip.clip_id] = (
                    f"physical SRT with {adapter.srt_requirement} coverage is missing"
                )
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
    ) -> EnqueueTrajectoryResult:
        preflight = self.preflight_trajectory_jobs(project_id, clip_ids=clip_ids)
        confirmed = set(confirmed_clip_ids)
        invalid_confirmations = confirmed - set(preflight.needs_confirmation)
        if invalid_confirmations:
            raise ValueError(
                f"clips do not require confirmation: {sorted(invalid_confirmations)}"
            )
        accepted_ids = (*preflight.eligible, *(item for item in preflight.needs_confirmation if item in confirmed))
        clips_manifest = self.repositories.clips.load(project_id)
        project = self.repositories.project.load(project_id)
        by_id = {clip.clip_id: clip for clip in clips_manifest.clips}
        trajectory_ids: list[str] = []
        for clip_id in accepted_ids:
            clip = by_id[clip_id]
            adapter = self.adapters.for_workflow(str(clip.resolved_workflow))
            physical_clip = _clip_output_path(clip)
            dependency_ids: tuple[str, ...] = ()
            if physical_clip is None or not physical_clip.is_file():
                export = self._new_job(
                    project_id,
                    clip,
                    job_type="clip_export",
                    resource_class="media_io",
                    adapter_name="clip_export",
                    adapter_version="1",
                    exclusive_key=f"export:{project_id}:{clip.clip_id}",
                    dependency_ids=(),
                    project_assets=project.source_assets,
                    project_revision=project.revision,
                    clips_revision=clips_manifest.revision,
                )
                submitted_export = self.queue.submit(export)
                if submitted_export.job_id == export.job_id:
                    Path(submitted_export.attempts[-1].directory).mkdir(
                        parents=True, exist_ok=False
                    )
                dependency_ids = (submitted_export.job_id,)
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
        self._publish_queue(project_id)
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
        current_fingerprint: str,
    ) -> QueueJob:
        current = self.queue.get(job_id)
        if current.project_id != project_id:
            raise KeyError(f"job {job_id} does not belong to {project_id}")
        if current_fingerprint != current.input_fingerprint:
            finished = self.queue.mark_stale_input(job_id)
        elif result.status != "success":
            finished = self.queue.mark_failed(
                job_id, result.error or "adapter execution failed"
            )
        else:
            if result.output_revision is None or result.output_fingerprint is None:
                finished = self.queue.mark_failed(
                    job_id, "adapter returned an unvalidated output identity"
                )
            else:
                if current.status == "running":
                    self.queue.mark_validating(job_id)
                finished = self.queue.mark_success(
                    job_id,
                    output_revision=result.output_revision,
                    output_fingerprint=result.output_fingerprint,
                    output_validated=True,
                    published_outputs=result.outputs,
                )
        self._publish_queue(project_id)
        return finished

    def cancel_job(self, project_id: str, job_id: str) -> QueueJob:
        current = self.queue.get(job_id)
        if current.project_id != project_id:
            raise KeyError(f"job {job_id} does not belong to {project_id}")
        cancelled = self.queue.cancel(job_id)
        self._publish_queue(project_id)
        return cancelled

    def retry_job(self, project_id: str, job_id: str) -> QueueJob:
        current = self.queue.get(job_id)
        if current.project_id != project_id:
            raise KeyError(f"job {job_id} does not belong to {project_id}")
        number = len(current.attempts) + 1
        directory = self._attempt_directory(project_id, job_id, number)
        directory.mkdir(parents=True, exist_ok=False)
        retried = self.queue.retry(
            job_id,
            AttemptRecord(number=number, directory=str(directory)),
        )
        self._publish_queue(project_id)
        return retried

    def restore_jobs(
        self,
        project_id: str,
        *,
        process_probe: Callable[[int], Mapping[str, object] | None] | None = None,
    ) -> LocalResourceQueue:
        manifest = self.repositories.jobs.load(project_id)
        restored = LocalResourceQueue.restore(
            manifest.jobs,
            queue_order=manifest.queue_order,
            capacities=self.queue.capacities,
            process_probe=process_probe,
        )
        self.queue = restored
        self._publish_queue(project_id)
        return restored

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
        identity_payload = {
            "job_type": job_type,
            "clip_id": clip.clip_id,
            "clip_interval": _authoritative_interval(clip),
            "analysis_revision": clip.analysis_revision,
            "resolved_workflow": clip.resolved_workflow,
            "source_assets": _asset_fingerprints(project_assets),
            "project_manifest_revision": project_revision,
            "clips_manifest_revision": clips_revision,
            "adapter_name": adapter_name,
            "adapter_version": adapter_version,
            "parameters": dict(clip.manual_definition),
        }
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

    def _attempt_directory(
        self, project_id: str, job_id: str, number: int
    ) -> Path:
        return self.projects_root / project_id / "jobs" / job_id / f"attempt-{number}"

    def _publish_queue(self, project_id: str) -> JobsManifest:
        current = self.repositories.jobs.load(project_id)
        jobs = tuple(
            job.to_dict()
            for job in self.queue.jobs()
            if job.project_id == project_id
        )
        order = tuple(
            job_id
            for job_id in self.queue.queue_order()
            if self.queue.get(job_id).project_id == project_id
        )
        return self.repositories.jobs.update(
            project_id,
            expected_revision=current.revision,
            mutate=lambda manifest: replace(
                manifest,
                jobs=jobs,
                queue_order=order,
            ),
        )


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


def _clip_output_path(clip: ClipDefinition) -> Path | None:
    for key in ("physical_mp4_path", "export_path", "clip_path"):
        value = clip.analysis.get(key)
        if value:
            return Path(str(value))
    return None


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


def _fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
