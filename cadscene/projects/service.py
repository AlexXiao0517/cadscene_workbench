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
from .executor import JobExecutionPlan
from .json_repositories import ProjectRepositories
from .models import ClipDefinition, ClipsManifest, JobsManifest, RenderManifest
from .repositories import ManifestMutation, RevisionConflict, publish_manifests
from .queue import (
    AttemptRecord,
    LocalResourceQueue,
    QueueJob,
    RestoreCleanupReservation,
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
        self._publication_lock = threading.RLock()
        self.queue.enable_publication_gate()

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
        with self._state_guard(project_id):
            current = self.queue.get(job_id)
            if current.project_id != project_id:
                raise KeyError(f"job {job_id} does not belong to {project_id}")
            authoritative_fingerprint = self._current_input_fingerprint(current)
            lease = {
                "attempt_number": attempt_number,
                "claim_token": claim_token,
            }
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
                self._publish_queue_locked(project_id)
                raise RuntimeError("job input changed before execution")
        if job.job_type == "clip_export":
            return self._prepare_clip_export(job)
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
            srt_path=_asset_path(project.source_assets, "srt"),
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
            restored = self.queue.merge_restored(
                manifest.jobs,
                queue_order=manifest.queue_order,
                process_probe=process_probe,
                current_fingerprint_resolver=self._current_input_fingerprint,
                defer_cleanup=True,
                unverified_process_policy=unverified_process_policy,
            )
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

    def reap_adopted_jobs(self) -> tuple[str, ...]:
        with self._publication_lock:
            reaped = self.queue.poll_adopted_processes()
            project_ids = {
                self.queue.get(job_id).project_id for job_id in reaped
            } | set(self.queue.pending_publication_projects())
            failures: list[tuple[str, Exception]] = []
            for project_id in sorted(project_ids):
                try:
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
        video_path = _asset_path(project.source_assets, "video")
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
    return {
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


def _export_identity_payload(
    *,
    clip: ClipDefinition,
    project_assets: Mapping[str, object],
    project_revision: int,
    clips_revision: int,
) -> Mapping[str, object]:
    return {
        "job_type": "clip_export",
        "clip_id": clip.clip_id,
        "analysis_revision": clip.analysis_revision,
        "clip_interval": _authoritative_interval(clip),
        "source_assets": _asset_fingerprints(project_assets),
        "project_manifest_revision": project_revision,
        "clips_manifest_revision": clips_revision,
        "adapter_name": "clip_export",
        "adapter_version": "1",
    }


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
