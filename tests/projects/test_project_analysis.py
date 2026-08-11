from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cadscene.projects.analysis import ProjectAnalysisCoordinator
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
from cadscene.projects.uploads import PublishedUpload
from cadscene.projects.workflow_adapters import default_workflow_adapters


def _coordinator(tmp_path: Path) -> tuple[ProjectAnalysisCoordinator, LocalResourceQueue]:
    projects_root = tmp_path / "projects"
    repositories = project_repositories(projects_root)
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "video.mp4"
    cad = tmp_path / "design.json"
    video.write_bytes(b"video")
    cad.write_text("{}", encoding="utf-8")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(video), "sha256": "a" * 64},
                "cad": {"path": str(cad), "sha256": "b" * 64},
                "_analysis": {"request_key": "request-1", "status": "queued"},
            },
        ),
    )
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: "later",
    )
    return ProjectAnalysisCoordinator(service), queue


def _upload(tmp_path: Path, *, project_id: str = "p1") -> PublishedUpload:
    path = tmp_path / "video.mp4"
    report = tmp_path / "video.validation.json"
    report.write_text("{}", encoding="utf-8")
    return PublishedUpload(
        project_id=project_id,
        asset_type="video",
        original_filename="video.mp4",
        path=path,
        size_bytes=path.stat().st_size,
        sha256="a" * 64,
        validation={},
        validation_report_path=report,
    )


def test_trigger_returns_persisted_dag_without_future_or_private_executor(
    tmp_path: Path,
) -> None:
    coordinator, queue = _coordinator(tmp_path)

    result = coordinator.trigger("p1", "video", _upload(tmp_path))

    assert len(result.job_ids) == 2
    assert tuple(job.job_id for job in queue.jobs()) == result.job_ids
    assert not hasattr(result, "result")
    assert not hasattr(coordinator, "_executor")


def test_trigger_rejects_cross_project_upload(tmp_path: Path) -> None:
    coordinator, _queue = _coordinator(tmp_path)

    with pytest.raises(ValueError, match="another project"):
        coordinator.trigger("p1", "video", _upload(tmp_path, project_id="p2"))
