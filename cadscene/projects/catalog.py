from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProjectSummary:
    project_id: str
    revision: int | None
    display_name: str
    updated_at: str | None
    video_filename: str | None
    cad_filename: str | None
    clip_count: int
    rendered_clip_count: int
    running_job_count: int
    status: str
    openable: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


_RUNNING = {"queued", "preparing", "running", "validating", "publishing"}
_FAILED = {"failed", "cancelled", "stale_input", "superseded"}


def build_project_summary(project, clips, jobs, render) -> ProjectSummary:
    jobs_by_clip: dict[str, Mapping[str, object]] = {}
    for job in jobs.jobs:
        clip_id = job.get("clip_id")
        if isinstance(clip_id, str) and job.get("job_type") == "clip_render":
            jobs_by_clip[clip_id] = job

    renders_by_clip: dict[str, Mapping[str, object]] = {}
    for item in render.clip_renders:
        clip_id = item.get("clip_id")
        if isinstance(clip_id, str):
            renders_by_clip[clip_id] = item

    rendered_count = sum(
        _render_is_current(jobs_by_clip.get(clip.clip_id), renders_by_clip.get(clip.clip_id))
        for clip in clips.clips
    )
    statuses = {str(job.get("status") or "") for job in jobs.jobs}
    running_count = sum(str(job.get("status") or "") in _RUNNING for job in jobs.jobs)
    if running_count:
        status = "processing"
    elif statuses & _FAILED:
        status = "failed"
    elif clips.clips and rendered_count == len(clips.clips):
        status = "completed"
    elif clips.clips:
        status = "ready"
    else:
        status = "new"
    return ProjectSummary(
        project_id=project.project_id,
        revision=project.revision,
        display_name=str(project.source_assets.get("display_name") or project.project_id),
        updated_at=project.updated_at,
        video_filename=_asset_filename(project.source_assets, "video"),
        cad_filename=_asset_filename(project.source_assets, "cad"),
        clip_count=len(clips.clips),
        rendered_clip_count=rendered_count,
        running_job_count=running_count,
        status=status,
    )


def unavailable_project_summary(project_id: str) -> ProjectSummary:
    return ProjectSummary(
        project_id=project_id,
        revision=None,
        display_name=project_id,
        updated_at=None,
        video_filename=None,
        cad_filename=None,
        clip_count=0,
        rendered_clip_count=0,
        running_job_count=0,
        status="unavailable",
        openable=False,
    )


def sort_project_summaries(values: Sequence[ProjectSummary]) -> tuple[ProjectSummary, ...]:
    return tuple(
        sorted(values, key=lambda item: (item.updated_at or "", item.project_id), reverse=True)
    )


def _render_is_current(
    job: Mapping[str, object] | None,
    render: Mapping[str, object] | None,
) -> bool:
    if job is None or render is None:
        return False
    if job.get("status") != "success" or render.get("status") != "success":
        return False
    job_revision = job.get("output_revision")
    render_revision = render.get("output_revision")
    return bool(job_revision) and job_revision == render_revision


def _asset_filename(source_assets: Mapping[str, object], asset_type: str) -> str | None:
    descriptor = source_assets.get(asset_type)
    if isinstance(descriptor, Mapping):
        for key in ("original_filename", "original_name", "filename", "path"):
            value = descriptor.get(key)
            if isinstance(value, str) and value:
                return Path(value).name
    legacy = source_assets.get(f"{asset_type}_path")
    return Path(legacy).name if isinstance(legacy, str) and legacy else None
