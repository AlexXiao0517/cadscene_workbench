from __future__ import annotations

from dataclasses import replace
from http.client import HTTPConnection
from io import BytesIO
import json
from pathlib import Path
import threading

from cadscene.projects.http_api import ProjectApi, UploadRequest
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, StateReference, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.service import ProjectService
from cadscene.projects.uploads import ValidatedUploadStore
from cadscene.projects.workflow_adapters import default_workflow_adapters
from cadscene.cli.serve_viewer import RangeRequestHandler, ViewerHTTPServer
from cadscene.projects.http_api import ApiResponse


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
    video.write_bytes(b"video")
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
    assert by_id["ready"]["display_name"] == "场景 02 · 第 1 段"
    assert by_id["ready"]["time_range"] == "01:30 – 02:30"
    assert by_id["ready"]["duration"] == "01:00"
    assert by_id["ready"]["capabilities"]["can_start_trajectory"] is True
    assert by_id["full-pose"]["recommended_workflow"] == "srt_full_pose"
    assert by_id["full-pose"]["capabilities"]["can_start_trajectory"] is False
    assert "interface-only" in by_id["full-pose"]["capabilities"]["reason"]


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


def test_unknown_project_route_is_rejected_by_project_api(tmp_path: Path) -> None:
    api, _repositories, _queue = _api(tmp_path, (_clip("clip-1"),))

    response = api.handle("GET", "/api/projects/p1/not-a-route")

    assert response.status == 404
    assert response.body == {"error": "project_api_not_found"}


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


def test_analysis_triggers_once_only_after_all_required_assets_publish(
    tmp_path: Path,
) -> None:
    api, repositories, _queue = _api(tmp_path, (_clip("clip-1"),))
    triggers: list[tuple[str, str]] = []
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
        analysis_trigger=lambda project_id, kind, _upload: triggers.append(
            (project_id, kind)
        ),
    )
    revision = repositories.project.load("p1").revision
    video = api.handle(
        "POST",
        "/api/projects/p1/uploads/video",
        json_body={"expected_revision": revision},
        upload=UploadRequest("new.mp4", BytesIO(b"video"), 5),
    )
    assert video.status == 201
    assert triggers == []

    cad = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad",
        json_body={"expected_revision": video.body["project_revision"]},
        upload=UploadRequest("design.dxf", BytesIO(b"0\nEOF"), 5),
    )
    assert cad.status == 201
    assert triggers == [("p1", "cad")]

    repeated = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad",
        json_body={"expected_revision": cad.body["project_revision"]},
        upload=UploadRequest("design.dxf", BytesIO(b"0\nEOF"), 5),
    )
    assert repeated.status == 201
    assert triggers == [("p1", "cad")]
