from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from cadscene.projects.adapters import AdapterProgress
from cadscene.projects.http_api import ProjectApi, UploadRequest
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, StateReference, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService, RegisterUploadResult
from cadscene.projects.service import (
    EnqueueRenderResult,
    RenderPreflight,
    TrajectoryPreflight,
)
from cadscene.projects.uploads import ValidatedUploadStore
from cadscene.projects.workflow_adapters import default_workflow_adapters
from cadscene.cli.serve_viewer import (
    RangeRequestHandler,
    ViewerHTTPServer,
    _cad_thumbnail_svg,
)
from cadscene.projects.http_api import ApiResponse


def test_cad_thumbnail_svg_renders_actual_design_geometry() -> None:
    svg = _cad_thumbnail_svg(
        {
            "meta": {"width": 100.0, "height": 50.0},
            "layers": [
                {
                    "entities": [
                        {"points": [[0.0, 0.0], [50.0, 25.0], [100.0, 50.0]]}
                    ]
                }
            ],
        }
    )

    assert svg.startswith(b"<svg")
    assert b'viewBox="0 0 1200 720"' in svg
    assert b"<path" in svg
    assert b"DXF" not in svg


def test_serve_viewer_wires_durable_project_runtime_and_real_queue_executor() -> None:
    source = (Path(__file__).resolve().parents[2] / "cadscene" / "cli" / "serve_viewer.py").read_text(
        encoding="utf-8"
    )

    assert "LocalJobExecutor(project_service)" in source
    assert "ProjectRuntime(" in source
    assert "project_runtime.start()" in source
    assert "project_runtime.close()" in source
    assert "ProjectAnalysisCoordinator" not in source
    assert "analysis_trigger=" not in source
    assert "analysis=None" in source
    assert "AtomicWorkbenchSessionStore(projects_root)" in source
    assert "ProjectWorkbenchService(" in source
    assert "workbench=project_workbench" in source
    assert "default_workbench_render_adapters(" in source
    assert "render_adapters=default_workbench_render_adapters" in source


def _clip(
    clip_id: str,
    *,
    workflow: str = "sfm_only",
    needs_review: bool = False,
) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "source_start_pts": 2250,
            "source_end_pts_exclusive": 3750,
            "source_start_pts_sec": 90.0,
            "source_end_pts_exclusive_sec": 150.0,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "scene_index": 2,
            "segment_index": 1,
            "detected_motion_mode": "general_motion",
            "confidence": 0.87,
            "recommended_workflow": workflow,
            "needs_review": needs_review,
        },
        generated_display_name="场景 02 · 第 1 段",
        references=(
            StateReference(
                owner="jobs",
                key=f"trajectory:{clip_id}",
                operation_id="old-trajectory",
                value={"status": "success", "output_revision": "trajectory-1"},
            ),
            StateReference(
                owner="render",
                key=f"render:{clip_id}",
                operation_id="old-render",
                value={"status": "success", "output_revision": "render-1"},
            ),
        ),
    )


def _api(tmp_path: Path, clips: tuple[ClipDefinition, ...]):
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="2026-08-04T00:00:00Z")
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.dxf"
    video.write_bytes(b"video")
    cad.write_bytes(b"cad")
    (tmp_path / "video.validation.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cad.validation.json").write_text("{}", encoding="utf-8")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            register_analysis_revision(
                current, "analysis-1", operation_id="analysis-operation"
            ),
            source_assets={"video_path": str(video)},
            project_state="ready",
        ),
    )
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=clips
        ),
    )
    queue = LocalResourceQueue()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:01Z",
    )
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(
            tmp_path / "projects", validators={"video": lambda *_: {"ok": True}}
        ),
        now=lambda: "2026-08-04T00:00:01Z",
    )
    return api, repositories, queue


def test_snapshot_etag_returns_304_with_an_empty_body(tmp_path: Path) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    first = api.handle("GET", "/api/projects/p1/snapshot")
    second = api.handle(
        "GET",
        "/api/projects/p1/snapshot",
        headers={"If-None-Match": first.headers["ETag"]},
    )

    assert first.status == 200
    assert first.body["snapshot_revision"]
    assert second.status == 304
    assert second.body is None
    assert second.encoded_body == b""

    lowercase = api.handle(
        "GET",
        "/api/projects/p1/snapshot",
        headers={"if-none-match": first.headers["ETag"]},
    )
    assert lowercase.status == 304
    assert lowercase.encoded_body == b""


def test_annotation_crud_api_and_snapshot_use_independent_revisions(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    payload = {
        "expected_revision": 0,
        "annotation_id": "label-1",
        "clip_id": "clip-1",
        "anchor_type": "cad_anchor",
        "text": "K12+340",
        "anchor": {"cad_world_xyz": [1.0, 2.0, 3.0]},
        "source_pts_range": {
            "start_pts": 2250,
            "end_pts_exclusive": 3750,
            "time_base": {"numerator": 1, "denominator": 25},
            "semantics": "half_open",
        },
    }

    created = api.handle(
        "POST", "/api/projects/p1/annotations", json_body=payload
    )
    snapshot = api.handle("GET", "/api/projects/p1/snapshot")
    updated = api.handle(
        "PATCH",
        "/api/projects/p1/annotations/label-1",
        json_body={
            "expected_revision": 1,
            "expected_annotation_revision": 0,
            "changes": {"text": "K12+360", "screen_offset": [14.0, -9.0]},
        },
    )
    deleted = api.handle(
        "DELETE",
        "/api/projects/p1/annotations/label-1",
        json_body={
            "expected_revision": 2,
            "expected_annotation_revision": 1,
        },
    )

    assert created.status == 201
    assert created.body["annotation"]["annotation_id"] == "label-1"
    assert created.body["annotations_revision"] == 1
    assert snapshot.body["component_revisions"]["annotations"] == 1
    assert snapshot.body["annotations"][0]["text"] == "K12+340"
    assert updated.status == 200
    assert updated.body["annotation"]["annotation_revision"] == 1
    assert updated.body["annotation"]["text"] == "K12+360"
    assert deleted.status == 200
    assert deleted.body["annotations_revision"] == 3
    assert repositories.annotations.load("p1").annotations == ()


def test_snapshot_etag_changes_when_media_loss_changes_server_capabilities(
    tmp_path: Path,
) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    first = api.handle("GET", "/api/projects/p1/snapshot")
    assert first.body["capabilities"]["can_start_trajectory"] is True
    assert first.body["clips"][0]["capabilities"]["can_start_trajectory"] is True
    (tmp_path / "source.mp4").unlink()

    changed = api.handle(
        "GET",
        "/api/projects/p1/snapshot",
        headers={"If-None-Match": first.headers["ETag"]},
    )

    assert changed.status == 200
    assert changed.headers["ETag"] != first.headers["ETag"]
    assert changed.body["capabilities"]["can_start_trajectory"] is False
    assert changed.body["clips"][0]["capabilities"]["can_start_trajectory"] is False


def test_snapshot_exposes_server_capabilities_and_product_friendly_clip_fields(
    tmp_path: Path,
) -> None:
    api, _repositories, _queue = _api(
        tmp_path,
        (
            _clip("ready"),
            _clip("full-pose", workflow="srt_full_pose"),
        ),
    )

    response = api.handle("GET", "/api/projects/p1/snapshot")
    by_id = {item["clip_id"]: item for item in response.body["clips"]}

    assert response.body["capabilities"]["can_merge"] is False
    assert response.body["display_name"] == "p1"
    assert response.body["assets"]["video"]["thumbnail_url"] == "/api/projects/p1/thumbnails/source"
    assert by_id["ready"]["display_name"] == "场景 02 · 第 1 段"
    assert by_id["ready"]["time_range"] == "01:30 – 02:30"
    assert by_id["ready"]["duration"] == "01:00"
    assert by_id["ready"]["thumbnail_url"] == "/api/projects/p1/thumbnails/clips/ready"
    assert by_id["ready"]["capabilities"]["can_start_trajectory"] is True
    assert by_id["full-pose"]["recommended_workflow"] == "srt_full_pose"
    assert by_id["full-pose"]["capabilities"]["can_start_trajectory"] is False
    assert "interface-only" in by_id["full-pose"]["capabilities"]["reason"]


def test_project_can_start_trajectory_when_only_review_confirmation_is_needed(
    tmp_path: Path,
) -> None:
    api, _repositories, _queue = _api(
        tmp_path, (_clip("review", needs_review=True),)
    )

    response = api.handle("GET", "/api/projects/p1/snapshot")

    assert response.body["capabilities"]["can_start_trajectory"] is True
    clip = response.body["clips"][0]
    assert clip["capabilities"]["trajectory_needs_confirmation"] is True


def test_analysis_queued_disables_all_trajectory_capabilities(tmp_path: Path) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("ready"),))
    project = repositories.project.load("p1")
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.dxf"
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(video), "sha256": "a" * 64},
                "cad": {"path": str(cad), "sha256": "b" * 64},
                "_analysis": {"request_key": "request", "status": "queued"},
            },
            project_state="analyzing",
        ),
    )

    snapshot = api.handle("GET", "/api/projects/p1/snapshot")

    assert snapshot.body["capabilities"]["can_start_trajectory"] is False
    assert snapshot.body["clips"][0]["capabilities"]["can_start_trajectory"] is False
    preflight = api.service.preflight_trajectory_jobs("p1")
    assert preflight.eligible == ()
    assert preflight.skipped == ("ready",)
    assert "analysis is still running" in preflight.reasons["ready"]
    enqueue = api.service.enqueue_trajectory_jobs("p1")
    assert enqueue.job_ids == ()


def test_snapshot_exposes_real_analysis_job_progress(tmp_path: Path) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("ready"),))
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {
                    "path": str(tmp_path / "source.mp4"),
                    "sha256": "a" * 64,
                    "original_filename": "source.mp4",
                },
                "cad": {
                    "path": str(tmp_path / "design.dxf"),
                    "sha256": "b" * 64,
                    "original_filename": "design.dxf",
                },
                "_analysis": {"request_key": "request-1", "status": "queued"},
            },
            project_state="analyzing",
        ),
    )
    enqueued = api.service.enqueue_analysis_jobs("p1")
    cad_job = queue.claim_next_unstarted()
    assert cad_job is not None and cad_job.job_id == enqueued.job_ids[0]
    attempt = cad_job.attempts[-1]
    api.service.update_job_progress(
        "p1",
        cad_job.job_id,
        AdapterProgress(
            stage="parsing_cad", message="正在解析 CAD", fraction=0.42
        ),
        attempt_number=attempt.number,
        claim_token=str(attempt.worker_claim_token),
    )

    snapshot = api.handle("GET", "/api/projects/p1/snapshot")

    assert snapshot.body["analysis"]["status"] == "parsing_cad"
    by_type = {
        item["job_type"]: item for item in snapshot.body["analysis"]["jobs"]
    }
    assert by_type["cad_analysis"]["progress"] == {
        "stage": "parsing_cad",
        "message": "正在解析 CAD",
        "fraction": 0.42,
    }
    assert by_type["video_analysis"]["depends_on_job_ids"] == [cad_job.job_id]


def test_snapshot_reads_all_manifests_under_fixed_lock_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    initial_clips_revision = repositories.clips.load("p1").revision
    begin_update = threading.Event()
    update_done = threading.Event()
    original_project_load = repositories.project.load
    first = True

    def delayed_project_load(project_id: str):
        nonlocal first
        value = original_project_load(project_id)
        if first:
            first = False
            begin_update.set()
            update_done.wait(0.2)
        return value

    monkeypatch.setattr(repositories.project, "load", delayed_project_load)

    def update_clips() -> None:
        begin_update.wait(1)
        current = repositories.clips.load("p1")
        repositories.clips.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(value, updated_at="changed"),
        )
        update_done.set()

    updater = threading.Thread(target=update_clips)
    updater.start()
    response = api.handle("GET", "/api/projects/p1/snapshot")
    updater.join(2)

    assert not updater.is_alive()
    assert response.body["component_revisions"]["clips"] == initial_clips_revision


def test_workflow_override_requires_revision_and_null_restores_recommendation(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    revision = repositories.clips.load("p1").revision

    changed = api.handle(
        "PATCH",
        "/api/projects/p1/clips/clip-1/workflow",
        json_body={
            "expected_revision": revision,
            "workflow_override": "pure_rotation",
        },
    )

    assert changed.status == 200
    stored = repositories.clips.load("p1").clips[0]
    assert stored.workflow_override == "pure_rotation"
    assert stored.resolved_workflow == "pure_rotation"
    assert all(ref.value["status"] == "stale" for ref in stored.references)
    assert all("output_revision" in ref.value for ref in stored.references)

    reset = api.handle(
        "PATCH",
        "/api/projects/p1/clips/clip-1/workflow",
        json_body={
            "expected_revision": changed.body["clips_revision"],
            "workflow_override": None,
        },
    )
    assert reset.status == 200
    restored = repositories.clips.load("p1").clips[0]
    assert restored.workflow_override is None
    assert restored.resolved_workflow == "sfm_only"

    conflict = api.handle(
        "PATCH",
        "/api/projects/p1/clips/clip-1/workflow",
        json_body={"expected_revision": revision, "workflow_override": None},
    )
    assert conflict.status == 409
    assert conflict.body["error"] == "revision_conflict"
    assert conflict.body["current_revision"] == reset.body["clips_revision"]


def test_batch_preflight_returns_per_clip_partial_result_without_enqueueing(
    tmp_path: Path,
) -> None:
    api, repositories, queue = _api(
        tmp_path,
        (
            _clip("ready"),
            _clip("review", needs_review=True),
            _clip("unsupported", workflow="srt_full_pose"),
        ),
    )

    response = api.handle(
        "POST",
        "/api/projects/p1/trajectory-jobs",
        json_body={
            "expected_revision": repositories.jobs.load("p1").revision,
            "clip_ids": ["ready", "review", "unsupported"],
            "enqueue": False,
        },
    )

    assert response.status == 200
    assert response.body["eligible"] == ["ready"]
    assert response.body["needs_confirmation"] == ["review"]
    assert response.body["skipped"] == ["unsupported"]
    assert queue.jobs() == ()


def test_render_preflight_and_enqueue_routes_delegate_to_project_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("ready"),))
    preflight = RenderPreflight(
        eligible=("ready",), confirmation_required=(), skipped=(), reasons={}
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        api.service,
        "preflight_render_jobs",
        lambda project_id, *, clip_ids=None: (
            calls.append(("preflight", project_id, clip_ids)) or preflight
        ),
    )
    monkeypatch.setattr(
        api.service,
        "enqueue_render_jobs",
        lambda project_id, **kwargs: (
            calls.append(("enqueue", project_id, kwargs))
            or EnqueueRenderResult(("ready",), ("render-job-1",), preflight)
        ),
    )
    revision = repositories.jobs.load("p1").revision

    checked = api.handle(
        "POST", "/api/projects/p1/render-jobs",
        json_body={"expected_revision": revision, "clip_ids": ["ready"], "enqueue": False},
    )
    enqueued = api.handle(
        "POST", "/api/projects/p1/render-jobs",
        json_body={"expected_revision": revision, "clip_ids": ["ready"], "enqueue": True},
    )

    assert checked.status == 200
    assert checked.body["eligible"] == ["ready"]
    assert checked.body["confirmation_required"] == []
    assert enqueued.status == 202
    assert enqueued.body["job_ids"] == ["render-job-1"]
    assert calls[-1][0] == "enqueue"


def test_snapshot_exposes_server_derived_render_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("ready"),))
    monkeypatch.setattr(
        api.service,
        "preflight_render_jobs",
        lambda _project_id, *, clip_ids=None: RenderPreflight(
            eligible=("ready",), confirmation_required=(), skipped=(), reasons={}
        ),
    )

    response = api.handle("GET", "/api/projects/p1/snapshot")

    assert response.status == 200
    assert response.body["capabilities"]["can_render"] is True
    assert response.body["clips"][0]["capabilities"]["can_render"] is True
    assert response.body["clips"][0]["render"]["status"] == "not_started"


def test_snapshot_exposes_revision_scoped_render_preview_url_for_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("ready"),))
    current = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            jobs=(
                {
                    "job_id": "render-job-1",
                    "job_type": "clip_render",
                    "clip_id": "ready",
                    "status": "success",
                    "stage": "success",
                    "progress": {"fraction": 1.0},
                    "output_revision": "render-abc123",
                    "depends_on_job_ids": [],
                },
            ),
        ),
    )
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "ready:render-abc123",
                    "clip_id": "ready",
                    "output_revision": "render-abc123",
                    "status": "success",
                    "outputs": {"video": "ignored-by-snapshot"},
                },
            ),
        ),
    )
    monkeypatch.setattr(
        api.service,
        "preflight_trajectory_jobs",
        lambda _project_id, *, clip_ids=None: TrajectoryPreflight(
            eligible=(), needs_confirmation=(), skipped=tuple(clip_ids or ()), reasons={}
        ),
    )
    monkeypatch.setattr(
        api.service,
        "preflight_render_jobs",
        lambda _project_id, *, clip_ids=None: RenderPreflight(
            eligible=(), confirmation_required=(), skipped=tuple(clip_ids or ()), reasons={}
        ),
    )

    response = api.handle("GET", "/api/projects/p1/snapshot")

    assert response.status == 200
    assert response.body["clips"][0]["render"]["preview_url"] == (
        "/api/projects/p1/clips/ready/renders/render-abc123/video"
    )


def test_snapshot_keeps_stale_published_render_preview_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("ready"),))
    jobs = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=jobs.revision,
        mutate=lambda value: replace(
            value,
            jobs=(
                {
                    "job_id": "render-job-1",
                    "job_type": "clip_render",
                    "clip_id": "ready",
                    "status": "superseded",
                    "stage": "superseded",
                    "progress": {"fraction": 1.0},
                    "output_revision": None,
                    "depends_on_job_ids": [],
                },
            ),
        ),
    )
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "ready:render-abc123",
                    "clip_id": "ready",
                    "output_revision": "render-abc123",
                    "status": "stale_input",
                    "outputs": {"video": "ignored-by-snapshot"},
                },
            ),
        ),
    )
    monkeypatch.setattr(
        api.service,
        "preflight_trajectory_jobs",
        lambda _project_id, *, clip_ids=None: TrajectoryPreflight(
            eligible=(), needs_confirmation=(), skipped=tuple(clip_ids or ()), reasons={}
        ),
    )
    monkeypatch.setattr(
        api.service,
        "preflight_render_jobs",
        lambda _project_id, *, clip_ids=None: RenderPreflight(
            eligible=(), confirmation_required=(), skipped=tuple(clip_ids or ()), reasons={}
        ),
    )

    response = api.handle("GET", "/api/projects/p1/snapshot")

    assert response.body["clips"][0]["render"]["status"] == "superseded"
    assert response.body["clips"][0]["render"]["preview_url"] == (
        "/api/projects/p1/clips/ready/renders/render-abc123/video"
    )
    assert response.body["clips"][0]["render"]["preview_is_current"] is False


def test_cancel_and_retry_job_routes_delegate_through_project_service(
    tmp_path: Path,
) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("ready"),))
    enqueued = api.handle(
        "POST",
        "/api/projects/p1/trajectory-jobs",
        json_body={
            "expected_revision": repositories.jobs.load("p1").revision,
            "clip_ids": ["ready"],
            "enqueue": True,
        },
    )
    job_id = enqueued.body["job_ids"][-1]
    snapshot = api.handle("GET", "/api/projects/p1/snapshot")
    assert snapshot.body["clips"][0]["capabilities"]["can_cancel"] is True

    cancelled = api.handle(
        "POST",
        f"/api/projects/p1/jobs/{job_id}/cancel",
        json_body={"expected_revision": repositories.jobs.load("p1").revision},
    )
    assert cancelled.status == 202
    assert queue.status(job_id) == "cancelled"

    retry_snapshot = api.handle("GET", "/api/projects/p1/snapshot")
    assert retry_snapshot.body["clips"][0]["capabilities"]["can_retry"] is True
    retried = api.handle(
        "POST",
        f"/api/projects/p1/jobs/{job_id}/retry",
        json_body={"expected_revision": repositories.jobs.load("p1").revision},
    )
    assert retried.status == 202
    assert queue.status(job_id) in {"queued", "preparing"}


def test_job_runtime_route_delegates_to_project_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("ready"),))
    monkeypatch.setattr(
        api.service,
        "job_runtime",
        lambda project_id, job_id: {
            "project_id": project_id,
            "job_id": job_id,
            "status": "running",
            "stage": "running",
            "workflow_status": {"operation": "sfm"},
            "lines": ["matching"],
        },
        raising=False,
    )

    response = api.handle("GET", "/api/projects/p1/jobs/job-1/runtime")

    assert response.status == 200
    assert response.body["job_id"] == "job-1"
    assert response.body["workflow_status"]["operation"] == "sfm"
    assert response.body["lines"] == ["matching"]


def test_cancelling_job_does_not_offer_a_second_cancel_action(tmp_path: Path) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("ready"),))
    api.handle(
        "POST",
        "/api/projects/p1/trajectory-jobs",
        json_body={
            "expected_revision": repositories.jobs.load("p1").revision,
            "clip_ids": ["ready"],
            "enqueue": True,
        },
    )
    job_id = next(item.job_id for item in queue.jobs() if item.job_type == "trajectory")
    queue.begin_cancel(job_id)
    api.service._publish_queue("p1")

    snapshot = api.handle("GET", "/api/projects/p1/snapshot")

    assert snapshot.body["clips"][0]["status"] == "cancelling"
    assert snapshot.body["clips"][0]["capabilities"]["can_cancel"] is False


def test_unknown_project_route_is_rejected_by_project_api(tmp_path: Path) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    response = api.handle("GET", "/api/projects/p1/not-a-route")

    assert response.status == 404
    assert response.body == {"error": "project_api_not_found"}


@pytest.mark.parametrize("project_id", [".", ".."])
def test_api_rejects_dot_project_ids_without_touching_parent(
    tmp_path: Path, project_id: str
) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    response = api.handle("POST", "/api/projects", json_body={"project_id": project_id})

    assert response.status == 400
    assert response.body == {"error": "invalid project_id"}


def test_create_project_returns_the_revision_after_persisting_display_name(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    response = api.handle(
        "POST",
        "/api/projects",
        json_body={"project_id": "new-project", "display_name": "金华项目"},
    )

    assert response.status == 201
    assert response.body["project_revision"] == 1
    assert repositories.project.load("new-project").source_assets["display_name"] == "金华项目"


def test_project_display_name_can_be_renamed_with_expected_revision(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    revision = repositories.project.load("p1").revision

    response = api.handle(
        "PATCH",
        "/api/projects/p1",
        json_body={"expected_revision": revision, "display_name": "  总线项目  "},
    )

    assert response.status == 200
    assert response.body == {
        "project_id": "p1",
        "project_revision": revision + 1,
        "display_name": "总线项目",
    }
    assert repositories.project.load("p1").source_assets["display_name"] == "总线项目"


@pytest.mark.parametrize("display_name", ["", "   ", "项" * 121])
def test_project_display_name_rejects_invalid_values(
    tmp_path: Path, display_name: str
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    response = api.handle(
        "PATCH",
        "/api/projects/p1",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "display_name": display_name,
        },
    )

    assert response.status == 400
    assert "display_name must contain 1 to 120 characters" in response.body["error"]


def test_project_display_name_rename_rejects_stale_revision(tmp_path: Path) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    current = repositories.project.load("p1").revision

    response = api.handle(
        "PATCH",
        "/api/projects/p1",
        json_body={"expected_revision": current - 1, "display_name": "旧请求"},
    )

    assert response.status == 409
    assert response.body["error"] == "revision_conflict"
    assert response.body["current_revision"] == current


def test_serve_viewer_only_dispatches_project_transport_to_project_api(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeProjectApi:
        def handle(self, method, path, **kwargs):
            calls.append((method, path, dict(kwargs.get("json_body") or {})))
            return ApiResponse(
                200,
                {"delegated": True},
                {"ETag": '"snapshot"', "Cache-Control": "no-store"},
            )

    server = ViewerHTTPServer(("127.0.0.1", 0), RangeRequestHandler)
    server.root_dir = tmp_path
    server.storage_root_dir = tmp_path
    server.extra_roots = {}
    server.project_api = FakeProjectApi()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "PATCH",
            "/api/projects/p1/clips/c1/workflow",
            body=json.dumps(
                {"expected_revision": 2, "workflow_override": "sfm_only"}
            ),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert response.getheader("ETag") == '"snapshot"'
    assert body == {"delegated": True}
    assert calls == [
        (
            "PATCH",
            "/api/projects/p1/clips/c1/workflow",
            {"expected_revision": 2, "workflow_override": "sfm_only"},
        )
    ]


def test_serve_viewer_serves_real_project_snapshot_over_http(tmp_path: Path) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    server = ViewerHTTPServer(("127.0.0.1", 0), RangeRequestHandler)
    server.project_api = api
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", "/api/projects/p1/snapshot")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert response.getheader("ETag")
    assert body["project_id"] == "p1"
    assert body["clips"][0]["clip_id"] == "clip-1"


def test_serve_viewer_streams_published_project_render_with_head_and_range(
    tmp_path: Path,
) -> None:
    rendered = tmp_path / "rendered.mp4"
    rendered.write_bytes(b"0123456789")

    class FakeRenderService:
        def published_render_video_path(self, project_id, clip_id, output_revision):
            assert (project_id, clip_id, output_revision) == (
                "p1",
                "clip-1",
                "render-1",
            )
            return rendered

    class FakeProjectApi:
        service = FakeRenderService()

    server = ViewerHTTPServer(("127.0.0.1", 0), RangeRequestHandler)
    server.root_dir = tmp_path
    server.storage_root_dir = tmp_path
    server.extra_roots = {}
    server.project_api = FakeProjectApi()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route = "/api/projects/p1/clips/clip-1/renders/render-1/video"
    try:
        connection = HTTPConnection(*server.server_address, timeout=5)
        connection.request("HEAD", route)
        head = connection.getresponse()
        head_body = head.read()
        connection.close()

        connection = HTTPConnection(*server.server_address, timeout=5)
        connection.request("GET", route, headers={"Range": "bytes=2-5"})
        partial = connection.getresponse()
        partial_body = partial.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert head.status == 200
    assert head.getheader("Content-Type") == "video/mp4"
    assert head.getheader("Content-Length") == "10"
    assert head_body == b""
    assert partial.status == 206
    assert partial.getheader("Content-Range") == "bytes 2-5/10"
    assert partial_body == b"2345"


def test_analysis_dag_is_published_once_only_after_explicit_start(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    api = ProjectApi(
        repositories=repositories,
        service=api.service,
        uploads=ValidatedUploadStore(
            tmp_path / "projects",
            validators={
                "video": lambda *_: {"decoded": True},
                "cad": lambda *_: {"parsed": True},
            },
        ),
        now=lambda: "2026-08-04T00:00:02Z",
    )
    revision = repositories.project.load("p1").revision
    video = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"expected_revision": revision},
        upload=UploadRequest("new.mp4", BytesIO(b"video"), 5),
    )
    assert video.status == 201

    cad = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad",
        json_body={"expected_revision": video.body["project_revision"]},
        upload=UploadRequest("design.dxf", BytesIO(b"0\nEOF"), 5),
    )
    assert cad.status == 201
    project = repositories.project.load("p1")
    assert set(project.source_assets) >= {"video", "cad"}
    assert "_analysis" not in project.source_assets
    assert api.service.queue.jobs() == ()

    started = api.handle(
        "POST",
        "/api/projects/p1/analysis/start",
        json_body={"expected_revision": cad.body["project_revision"]},
    )
    assert started.status == 202
    analysis = repositories.project.load("p1").source_assets["_analysis"]
    assert analysis["status"] == "queued"
    assert len(analysis["job_ids"]) == 2
    assert tuple(started.body["job_ids"]) == tuple(analysis["job_ids"])
    assert [job.job_type for job in api.service.queue.jobs()] == [
        "cad_analysis",
        "video_analysis",
    ]


def test_browser_uploads_can_register_parallel_assets_using_current_revision(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    api.uploads = ValidatedUploadStore(
        tmp_path / "projects",
        validators={
            "video": lambda *_: {"decoded": True},
            "cad": lambda *_: {"parsed": True},
        },
    )

    video = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"use_current_revision": True},
        upload=UploadRequest("source.mp4", BytesIO(b"video"), 5),
    )
    cad = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad",
        json_body={"use_current_revision": True},
        upload=UploadRequest("design.dxf", BytesIO(b"0\nEOF"), 5),
    )

    assert video.status == 201
    assert cad.status == 201
    project = repositories.project.load("p1")
    assert set(project.source_assets) >= {"video", "cad"}
    assert "_analysis" not in project.source_assets
    assert api.service.queue.jobs() == ()


def test_upload_registration_failure_leaves_no_canonical_or_queued_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("clip-1"),))
    revision = repositories.project.load("p1").revision
    monkeypatch.setattr(
        repositories.project,
        "_publish_prepared_unchecked",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("injected project publication failure")
        ),
    )

    with pytest.raises(OSError, match="injected project publication"):
        api.handle(
            "POST",
            "/api/projects/p1/uploads/video",
            json_body={"expected_revision": revision},
            upload=UploadRequest("replacement.mp4", BytesIO(b"new-video"), 9),
        )

    project = repositories.project.load("p1")
    assert "video" not in project.source_assets
    assert queue.jobs() == ()
    assert not api.uploads.validation_report_path("p1", "video").exists()


def test_explicit_analysis_start_recovers_dag_publication_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("clip-1"),))
    api.uploads = ValidatedUploadStore(
        tmp_path / "projects",
        validators={
            "video": lambda *_: {"decoded": True},
            "cad": lambda *_: {"parsed": True},
        },
    )
    revision = repositories.project.load("p1").revision
    video = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"expected_revision": revision},
        upload=UploadRequest("source.mp4", BytesIO(b"video"), 5),
    )
    cad = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad",
        json_body={"expected_revision": video.body["project_revision"]},
        upload=UploadRequest("design.dxf", BytesIO(b"0\nEOF"), 5),
    )

    assert cad.status == 201
    assert queue.jobs() == ()
    original = repositories.jobs._publish_prepared_unchecked
    failed_once = False

    def fail_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("injected jobs publication crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(repositories.jobs, "_publish_prepared_unchecked", fail_once)
    started = api.handle(
        "POST",
        "/api/projects/p1/analysis/start",
        json_body={"expected_revision": cad.body["project_revision"]},
    )

    assert started.status == 202
    state = repositories.project.load("p1").source_assets["_analysis"]
    job_ids = tuple(state["job_ids"])
    assert len(job_ids) == 2
    assert tuple(state["job_ids"]) == job_ids
    assert tuple(item.job_id for item in queue.jobs()) == job_ids
    assert tuple(
        str(item["job_id"]) for item in repositories.jobs.load("p1").jobs
    ) == job_ids


def test_canonical_cache_failure_does_not_undo_authoritative_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    revision = repositories.project.load("p1").revision
    monkeypatch.setattr(
        api.uploads,
        "_write_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected canonical cache failure")
        ),
    )

    response = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"expected_revision": revision},
        upload=UploadRequest("replacement.mp4", BytesIO(b"new-video"), 9),
    )

    assert response.status == 201
    asset = repositories.project.load("p1").source_assets["video"]
    assert Path(asset["path"]).read_bytes() == b"new-video"
    assert Path(asset["validation_report"]).is_file()
    assert not api.uploads.validation_report_path("p1", "video").exists()


def test_http_upload_does_not_take_repository_lock_before_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    revision = repositories.project.load("p1").revision
    called: list[str] = []
    monkeypatch.setattr(
        api.service,
        "register_uploaded_asset",
        lambda project_id, upload, expected_revision: (
            called.append(project_id)
            or RegisterUploadResult(expected_revision + 1, None)
        ),
    )
    monkeypatch.setattr(
        repositories.project,
        "lock_for",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("HTTP acquired repository lock")
        ),
    )

    response = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"expected_revision": revision},
        upload=UploadRequest("replacement.mp4", BytesIO(b"new-video"), 9),
    )

    assert response.status == 201
    assert called == ["p1"]

def test_manual_reanalysis_advances_revision_and_captures_a_new_request_key(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.dxf"
    video.write_bytes(b"video")
    cad.write_bytes(b"cad")
    current = repositories.project.load("p1")
    current = repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {
                    "path": str(video),
                    "sha256": "a" * 64,
                    "original_filename": "source.mp4",
                    "size_bytes": 5,
                    "validation_report": str(tmp_path / "video.validation.json"),
                },
                "cad": {
                    "path": str(cad),
                    "sha256": "b" * 64,
                    "original_filename": "design.dxf",
                    "size_bytes": 3,
                    "validation_report": str(tmp_path / "cad.validation.json"),
                    "analysis": {"entities": 1},
                },
                "_analysis": {"request_key": "automatic", "status": "success"},
            },
        ),
    )
    response = api.handle(
        "POST",
        "/api/projects/p1/analysis/start",
        json_body={"expected_revision": current.revision},
    )

    assert response.status == 202
    changed = repositories.project.load("p1")
    assert response.body["project_revision"] == changed.revision
    assert changed.source_assets["_analysis"]["request_key"] != "automatic"
    assert changed.source_assets["_analysis"]["status"] == "queued"
    assert len(response.body["job_ids"]) == 2
    assert (
        api.handle("GET", "/api/projects/p1/snapshot")
        .body["capabilities"]["can_reanalyze"]
        is False
    )


def test_activate_candidate_analysis_endpoint_requires_both_manifest_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    calls: list[tuple[object, ...]] = []

    def activate(project_id: str, **kwargs: object) -> object:
        calls.append((project_id, kwargs))
        return SimpleNamespace(
            analysis_revision="analysis-2",
            project_revision=7,
            clips_revision=5,
        )

    monkeypatch.setattr(api.service, "activate_candidate_analysis", activate, raising=False)

    response = api.handle(
        "POST",
        "/api/projects/p1/analysis/activate",
        json_body={
            "candidate_analysis_revision": "analysis-2",
            "expected_revision": 6,
            "expected_clips_revision": 4,
        },
    )

    assert response.status == 200
    assert response.body == {
        "project_id": "p1",
        "active_analysis_revision": "analysis-2",
        "project_revision": 7,
        "clips_revision": 5,
    }
    assert calls == [
        (
            "p1",
            {
                "candidate_analysis_revision": "analysis-2",
                "expected_project_revision": 6,
                "expected_clips_revision": 4,
            },
        )
    ]


def test_manual_reanalysis_recovers_jobs_manifest_publication_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, repositories, queue = _api(tmp_path, (_clip("clip-1"),))
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.dxf"
    current = repositories.project.load("p1")
    current = repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {
                    "path": str(video),
                    "sha256": "a" * 64,
                    "validation_report": str(tmp_path / "video.validation.json"),
                },
                "cad": {
                    "path": str(cad),
                    "sha256": "b" * 64,
                    "validation_report": str(tmp_path / "cad.validation.json"),
                },
                "_analysis": {"request_key": "automatic", "status": "success"},
            },
        ),
    )
    original = repositories.jobs._publish_prepared_unchecked
    failed_once = False

    def fail_once(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("injected jobs publication crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(repositories.jobs, "_publish_prepared_unchecked", fail_once)

    response = api.handle(
        "POST",
        "/api/projects/p1/analysis/start",
        json_body={"expected_revision": current.revision},
    )

    assert response.status == 202
    job_ids = tuple(response.body["job_ids"])
    state = repositories.project.load("p1").source_assets["_analysis"]
    assert tuple(state["job_ids"]) == job_ids
    assert tuple(item.job_id for item in queue.jobs()) == job_ids
    assert tuple(
        str(item["job_id"]) for item in repositories.jobs.load("p1").jobs
    ) == job_ids


def test_manual_reanalysis_missing_captured_report_does_not_mutate_project(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    video = tmp_path / "source.mp4"
    cad = tmp_path / "design.dxf"
    video.write_bytes(b"video")
    cad.write_bytes(b"cad")
    current = repositories.project.load("p1")
    current = repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {
                    "path": str(video),
                    "sha256": "a" * 64,
                    "validation_report": str(tmp_path / "missing.json"),
                },
                "cad": {"path": str(cad), "sha256": "b" * 64},
                "_analysis": {"request_key": "automatic", "status": "success"},
            },
        ),
    )

    response = api.handle(
        "POST",
        "/api/projects/p1/analysis/start",
        json_body={"expected_revision": current.revision},
    )

    assert response.status == 400
    unchanged = repositories.project.load("p1")
    assert unchanged.revision == current.revision
    assert unchanged.source_assets["_analysis"] == {
        "request_key": "automatic",
        "status": "success",
    }
