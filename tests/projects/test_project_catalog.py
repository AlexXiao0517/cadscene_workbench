from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
from cadscene.projects.workflow_adapters import default_workflow_adapters


def _clip(project_id: str, clip_id: str) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": project_id,
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "scene_index": 1,
            "segment_index": 1,
            "recommended_workflow": "sfm_only",
        }
    )


def _service(root: Path) -> tuple[ProjectService, object]:
    repositories = project_repositories(root)
    service = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=root,
        now=lambda: "2026-08-24T12:00:00Z",
    )
    return service, repositories


def _create_project(
    repositories,
    project_id: str,
    *,
    updated_at: str,
    rendered: bool,
) -> None:
    repositories.create_project(project_id, updated_at=updated_at)
    project = repositories.project.load(project_id)
    repositories.project.update(
        project_id,
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            updated_at=updated_at,
            source_assets={
                "display_name": f"项目 {project_id}",
                "video": {
                    "original_filename": "flight.mp4",
                    "path": f"D:/private/{project_id}/flight.mp4",
                },
                "cad": {
                    "original_filename": "design.dxf",
                    "path": f"D:/private/{project_id}/design.dxf",
                },
            },
            project_state="ready",
        ),
    )
    clips = repositories.clips.load(project_id)
    repositories.clips.update(
        project_id,
        expected_revision=clips.revision,
        mutate=lambda current: replace(
            current,
            updated_at=updated_at,
            analysis_revision="analysis-1",
            clips=(_clip(project_id, "clip-1"),),
        ),
    )
    if not rendered:
        return
    jobs = repositories.jobs.load(project_id)
    repositories.jobs.update(
        project_id,
        expected_revision=jobs.revision,
        mutate=lambda current: replace(
            current,
            jobs=(
                {
                    "job_id": "render-job-1",
                    "job_type": "clip_render",
                    "clip_id": "clip-1",
                    "status": "success",
                    "output_revision": "render-rev-1",
                },
            ),
        ),
    )
    render = repositories.render.load(project_id)
    repositories.render.update(
        project_id,
        expected_revision=render.revision,
        mutate=lambda current: replace(
            current,
            clip_renders=(
                {
                    "render_id": "render-1",
                    "clip_id": "clip-1",
                    "status": "success",
                    "output_revision": "render-rev-1",
                },
            ),
        ),
    )


def test_project_catalog_sorts_summaries_and_hides_absolute_paths(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    _create_project(
        repositories,
        "project-older",
        updated_at="2026-08-20T10:00:00Z",
        rendered=False,
    )
    _create_project(
        repositories,
        "project-newer",
        updated_at="2026-08-24T10:00:00Z",
        rendered=True,
    )

    payload = [summary.to_dict() for summary in service.list_projects()]

    assert [item["project_id"] for item in payload] == [
        "project-newer",
        "project-older",
    ]
    assert payload[0]["video_filename"] == "flight.mp4"
    assert payload[0]["cad_filename"] == "design.dxf"
    assert payload[0]["clip_count"] == 1
    assert payload[0]["rendered_clip_count"] == 1
    assert payload[0]["running_job_count"] == 0
    assert payload[0]["status"] == "completed"
    assert "D:/private" not in json.dumps(payload)


def test_project_catalog_isolates_a_malformed_manifest(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    _create_project(
        repositories,
        "project-good",
        updated_at="2026-08-24T10:00:00Z",
        rendered=False,
    )
    broken = tmp_path / "projects" / "project-broken"
    broken.mkdir(parents=True)
    (broken / "project_manifest.json").write_text("{not json", encoding="utf-8")

    payload = [summary.to_dict() for summary in service.list_projects()]
    unavailable = next(item for item in payload if item["project_id"] == "project-broken")

    assert unavailable["status"] == "unavailable"
    assert unavailable["openable"] is False
    assert str(tmp_path) not in json.dumps(unavailable)


def test_current_success_is_not_overridden_by_historical_failed_jobs(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    _create_project(
        repositories,
        "project-recovered",
        updated_at="2026-08-24T10:00:00Z",
        rendered=True,
    )
    jobs = repositories.jobs.load("project-recovered")
    repositories.jobs.update(
        "project-recovered",
        expected_revision=jobs.revision,
        mutate=lambda current: replace(
            current,
            jobs=(
                {"job_id": "old-analysis", "job_type": "video_analysis", "status": "failed"},
                *current.jobs,
            ),
        ),
    )

    summary = service.list_projects()[0]

    assert summary.status == "completed"


def test_catalog_uses_latest_domain_manifest_update_time(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    _create_project(
        repositories,
        "project-active",
        updated_at="2026-08-20T10:00:00Z",
        rendered=False,
    )
    annotations = repositories.annotations.load("project-active")
    repositories.annotations.update(
        "project-active",
        expected_revision=annotations.revision,
        mutate=lambda current: replace(current, updated_at="2026-08-24T15:30:00Z"),
    )
    latest_annotation_time = repositories.annotations.load("project-active").updated_at

    summary = service.list_projects()[0]

    assert summary.updated_at == latest_annotation_time


def test_catalog_sanitizes_foreign_windows_asset_paths(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    repositories.create_project("project-migrated", updated_at="2026-08-24T10:00:00Z")
    project = repositories.project.load("project-migrated")
    repositories.project.update(
        "project-migrated",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={
                "video": {"path": r"D:\private\site\flight.mp4"},
                "cad": {"path": "/srv/private/site/design.dxf"},
            },
        ),
    )

    payload = service.list_projects()[0].to_dict()

    assert payload["video_filename"] == "flight.mp4"
    assert payload["cad_filename"] == "design.dxf"
    assert "private" not in json.dumps(payload)


def test_catalog_counts_every_active_job_even_with_the_same_status(tmp_path: Path) -> None:
    service, repositories = _service(tmp_path / "projects")
    _create_project(
        repositories,
        "project-busy",
        updated_at="2026-08-24T10:00:00Z",
        rendered=False,
    )
    jobs = repositories.jobs.load("project-busy")
    repositories.jobs.update(
        "project-busy",
        expected_revision=jobs.revision,
        mutate=lambda current: replace(
            current,
            jobs=(
                {"job_id": "analysis-running", "job_type": "video_analysis", "status": "running"},
                {"job_id": "render-running", "job_type": "clip_render", "clip_id": "clip-1", "status": "running"},
            ),
        ),
    )

    summary = service.list_projects()[0]

    assert summary.running_job_count == 2
    assert summary.status == "processing"
