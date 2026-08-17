from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.analysis_adapters import prepare_analysis_plan
from cadscene.projects.http_api import ProjectApi, UploadRequest
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition, StateReference, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue, QueueJob
from cadscene.projects.service import ProjectService
from cadscene.projects.service import _workbench_render_parameters
from cadscene.projects.uploads import PublishedUpload, ValidatedUploadStore
from cadscene.projects.workflow_adapters import default_workflow_adapters


def _write_workbench_output(
    projects_root: Path, *, project_id: str, clip: ClipDefinition
) -> StateReference:
    revision = "workbench-clip-1"
    operation_id = "save-workbench-1"
    target = projects_root / project_id / "workbench_outputs" / revision
    artifacts = target / "artifacts"
    artifacts.mkdir(parents=True)
    artifact = artifacts / "camera_track.json"
    artifact.write_bytes(b"validated-camera-track")
    artifact_hash = sha256(artifact.read_bytes()).hexdigest()
    payload = {
        "schema_version": "1.0",
        "project_id": project_id,
        "clip_id": clip.clip_id,
        "workflow": clip.resolved_workflow,
        "workbench_output_revision": revision,
        "operation_id": operation_id,
        "artifacts": {
            "camera_track": {
                "path": "artifacts/camera_track.json",
                "sha256": artifact_hash,
                "size_bytes": artifact.stat().st_size,
            }
        },
    }
    serialized = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fingerprint = sha256(serialized).hexdigest()
    payload["workbench_output_fingerprint"] = fingerprint
    (target / "workbench_output_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return StateReference(
        owner="clips",
        key=f"workbench:{clip.clip_id}",
        operation_id=operation_id,
        value={
            "status": "saved",
            "workbench_output_revision": revision,
            "workbench_output_fingerprint": fingerprint,
        },
    )


def _system(tmp_path: Path, *, with_saved_workbench: bool = True):
    projects_root = tmp_path / "projects"
    repositories = project_repositories(projects_root)
    repositories.create_project("p1", updated_at="2026-08-17T09:00:00Z")
    old_cad = tmp_path / "old.dxf"
    old_cad.write_bytes(b"old-cad")
    old_dataset = tmp_path / "old-cad-dataset"
    old_dataset.mkdir()
    (old_dataset / "design.json").write_text("{}", encoding="utf-8")
    project = repositories.project.load("p1")
    project = repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            register_analysis_revision(
                value, "analysis-1", operation_id="analysis-operation"
            ),
            source_assets={
                "cad": {
                    "path": str(old_cad),
                    "original_filename": "old.dxf",
                    "sha256": sha256(old_cad.read_bytes()).hexdigest(),
                }
            },
            project_state="ready",
        ),
    )
    clip = ClipDefinition.from_analysis(
        {
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "source_start_pts": 0,
            "source_end_pts_exclusive": 50,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "interval_semantics": "half_open",
            "recommended_workflow": "sfm_only",
            "input_snapshot": {
                "request_key": "analysis-request-1",
                "video": {"path": str(tmp_path / "video.mp4"), "sha256": "v" * 64},
                "cad": {
                    "path": str(old_cad),
                    "sha256": sha256(old_cad.read_bytes()).hexdigest(),
                    "dataset_id": "cad-old",
                    "dataset_path": str(old_dataset),
                },
                "srt": None,
                "analysis_artifact": {
                    "path": str(tmp_path / "analysis.json"),
                    "artifact_id": "analysis-artifact-1",
                },
            },
        }
    )
    if with_saved_workbench:
        clip = replace(
            clip,
            references=(
                _write_workbench_output(
                    projects_root, project_id="p1", clip=clip
                ),
            ),
        )
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(clip,)
        ),
    )
    queue = LocalResourceQueue()
    identities = iter(f"identity-{index}" for index in range(100))
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: "2026-08-17T09:00:01Z",
        identity=lambda: next(identities),
    )
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(
            projects_root,
            validators={"cad": lambda _path, _kind: {"format": "dxf"}},
        ),
        now=lambda: "2026-08-17T09:00:01Z",
    )
    return service, repositories, queue, api


def _published_cad(tmp_path: Path) -> PublishedUpload:
    path = tmp_path / "new.dxf"
    path.write_bytes(b"new-cad")
    report = tmp_path / "new-report.json"
    report.write_text("{}", encoding="utf-8")
    return PublishedUpload(
        project_id="p1",
        asset_type="cad",
        original_filename="new.dxf",
        path=path,
        size_bytes=path.stat().st_size,
        sha256=sha256(path.read_bytes()).hexdigest(),
        validation={"format": "dxf"},
        validation_report_path=report,
    )


def test_cad_replacement_requires_a_valid_saved_workbench_output(tmp_path: Path):
    service, _repositories, _queue, _api = _system(
        tmp_path, with_saved_workbench=False
    )

    eligibility = service.cad_replacement_eligibility("p1")

    assert eligibility == {
        "eligible": False,
        "reason": "项目尚未完成坐标系标定",
    }
    with pytest.raises(ValueError, match="坐标系"):
        service.request_cad_replacement(
            "p1",
            _published_cad(tmp_path),
            expected_revision=service.repositories.project.load("p1").revision,
            same_coordinate_system_confirmed=True,
        )


def test_api_queues_candidate_without_replacing_active_cad(tmp_path: Path):
    _service, repositories, queue, api = _system(tmp_path)
    before = repositories.project.load("p1")
    request = UploadRequest(
        filename="new.dxf",
        stream=__import__("io").BytesIO(b"new-cad"),
        size_bytes=len(b"new-cad"),
    )

    rejected = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad-replacement",
        json_body={"expected_revision": before.revision},
        upload=request,
    )

    assert rejected.status == 400
    assert "相同坐标系" in str(rejected.body["error"])

    accepted = api.handle(
        "POST",
        "/api/projects/p1/uploads/cad-replacement",
        json_body={
            "expected_revision": before.revision,
            "same_coordinate_system_confirmed": True,
        },
        upload=UploadRequest(
            filename="new.dxf",
            stream=__import__("io").BytesIO(b"new-cad"),
            size_bytes=len(b"new-cad"),
        ),
    )

    assert accepted.status == 202
    after = repositories.project.load("p1")
    assert after.source_assets["cad"]["original_filename"] == "old.dxf"
    replacement = after.source_assets["_cad_replacement"]
    assert replacement["candidate"]["original_filename"] == "new.dxf"
    assert replacement["status"] == "queued"
    job = queue.get(str(replacement["job_id"]))
    assert job.job_type == "cad_replacement"
    assert job.status in {"queued", "running"}
    snapshot = api.handle("GET", "/api/projects/p1/snapshot").body
    assert snapshot["cad_replacement"]["eligible"] is True
    assert snapshot["cad_replacement"]["status"] in {"queued", "running"}


def test_success_atomically_switches_cad_and_only_stales_render_outputs(
    tmp_path: Path,
):
    service, repositories, queue, _api = _system(tmp_path)
    project = repositories.project.load("p1")
    clips_before = repositories.clips.load("p1")
    jobs_before = repositories.jobs.load("p1")
    persisted_jobs_manifest = repositories.jobs.update(
        "p1",
        expected_revision=jobs_before.revision,
        mutate=lambda value: replace(
            value,
            jobs=(
                {
                    "job_id": "trajectory-existing",
                    "project_id": "p1",
                    "clip_id": "clip-1",
                    "job_type": "trajectory",
                    "resource_class": "heavy_compute",
                    "status": "success",
                    "stage": "success",
                    "priority": 0,
                    "depends_on_job_ids": [],
                    "exclusive_key": "trajectory:p1:clip-1",
                    "idempotency_key": "trajectory-key",
                    "input_revision": "analysis-1",
                    "input_fingerprint": "trajectory-input",
                    "adapter_name": "sfm_existing",
                    "adapter_version": "1",
                    "output_revision": "trajectory-output",
                    "output_fingerprint": "t" * 64,
                    "output_validated": True,
                    "validated_input_fingerprint": "trajectory-input",
                    "operation_id": "trajectory-op",
                    "attempts": [
                        {
                            "number": 1,
                            "directory": str(tmp_path / "trajectory-attempt"),
                            "pid": None,
                            "process_start_time": None,
                            "command_fingerprint": None,
                            "task_token": None,
                            "log_path": None,
                            "worker_claim_token": None,
                        }
                    ],
                    "published_outputs": {},
                },
            ),
        ),
    )
    queue.submit(QueueJob.from_dict(persisted_jobs_manifest.jobs[0]))
    queue.acknowledge_publication("p1")
    render = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=render.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "clip-1:render-old",
                    "clip_id": "clip-1",
                    "status": "success",
                },
            ),
            merge_plans=(
                {"merge_id": "merge-old", "status": "success"},
            ),
            published_outputs=(
                {"output_id": "merge-output-old", "status": "success"},
            ),
        ),
    )

    submitted = service.request_cad_replacement(
        "p1",
        _published_cad(tmp_path),
        expected_revision=project.revision,
        same_coordinate_system_confirmed=True,
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == submitted.job_id
    attempt = claimed.attempts[-1]
    assert attempt.worker_claim_token is not None
    service.update_job_progress(
        "p1",
        claimed.job_id,
        __import__("cadscene.projects.adapters", fromlist=["AdapterProgress"]).AdapterProgress(
            stage="running", fraction=0.5, message="正在导入新版 CAD"
        ),
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )
    plan = service.prepare_job_execution(
        "p1",
        claimed.job_id,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )
    assert "new.dxf" in plan.commands[0]

    scratch = Path(attempt.directory) / "scratch" / "data" / "p1"
    scratch.mkdir(parents=True)
    (scratch / "design.json").write_text("{}", encoding="utf-8")
    (scratch / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset": "p1",
                "cad": {"design_json": "data/p1/design.json"},
            }
        ),
        encoding="utf-8",
    )
    result = plan.validate()
    finished = service.finish_job(
        "p1",
        claimed.job_id,
        result,
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )

    assert finished.status == "success"
    active = repositories.project.load("p1").source_assets["cad"]
    assert active["original_filename"] == "new.dxf"
    assert active["dataset_id"].startswith("cad-")
    replacement = repositories.project.load("p1").source_assets[
        "_cad_replacement"
    ]
    assert replacement["status"] == "success"
    assert replacement["progress"]["fraction"] == 1.0
    assert repositories.clips.load("p1") == clips_before
    persisted_jobs = repositories.jobs.load("p1").jobs
    assert any(item["job_id"] == "trajectory-existing" for item in persisted_jobs)
    stale_render = repositories.render.load("p1")
    assert stale_render.clip_renders[0]["status"] == "stale_input"
    assert stale_render.merge_plans[0]["status"] == "stale_input"
    assert stale_render.published_outputs[0]["status"] == "stale_input"
    history = repositories.project.load("p1").source_assets["_cad_versions"]
    assert len(history) == 2
    parameters = _workbench_render_parameters(
        service.storage_root,
        "p1",
        clips_before.clips[0],
        repositories.project.load("p1").source_assets,
    )
    assert parameters["cad_dataset_path"] == active["dataset_path"]


def test_failed_replacement_keeps_the_previous_cad_active(tmp_path: Path):
    service, repositories, queue, _api = _system(tmp_path)
    before = repositories.project.load("p1")
    submitted = service.request_cad_replacement(
        "p1",
        _published_cad(tmp_path),
        expected_revision=before.revision,
        same_coordinate_system_confirmed=True,
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == submitted.job_id
    attempt = claimed.attempts[-1]
    assert attempt.worker_claim_token is not None

    finished = service.finish_job(
        "p1",
        claimed.job_id,
        AdapterResult.failed("invalid CAD geometry"),
        attempt_number=attempt.number,
        claim_token=attempt.worker_claim_token,
    )

    assert finished.status == "failed"
    project = repositories.project.load("p1")
    assert project.source_assets["cad"] == before.source_assets["cad"]
    assert project.source_assets["_cad_replacement"]["status"] == "failed"
    assert project.source_assets["_cad_replacement"]["error"] == "invalid CAD geometry"


def test_active_cad_change_does_not_change_trajectory_identity(tmp_path: Path):
    service, repositories, _queue, _api = _system(tmp_path)
    project = repositories.project.load("p1")
    clips = repositories.clips.load("p1")
    clip = clips.clips[0]
    adapter = service.adapters.for_workflow(str(clip.resolved_workflow))
    trajectory = service._new_job(
        "p1",
        clip,
        job_type="trajectory",
        resource_class="heavy_compute",
        adapter_name=adapter.name,
        adapter_version=adapter.version,
        exclusive_key="trajectory:p1:clip-1",
        dependency_ids=(),
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=clips.revision,
    )
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "cad": {
                    **value.source_assets["cad"],
                    "revision": "cad:new",
                    "dataset_id": "cad-new",
                    "dataset_path": str(tmp_path / "new-dataset"),
                },
            },
        ),
    )

    assert service._current_input_fingerprint(trajectory) == trajectory.input_fingerprint


def test_queued_replacement_restores_after_service_restart(tmp_path: Path):
    service, repositories, _queue, _api = _system(tmp_path)
    project = repositories.project.load("p1")
    submitted = service.request_cad_replacement(
        "p1",
        _published_cad(tmp_path),
        expected_revision=project.revision,
        same_coordinate_system_confirmed=True,
    )
    restarted_queue = LocalResourceQueue()
    restarted = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=service.projects_root,
        now=lambda: "2026-08-17T09:00:02Z",
    )

    restarted.restore_jobs("p1")
    claimed = restarted_queue.claim_next_unstarted()

    assert claimed is not None
    assert claimed.job_id == submitted.job_id
    assert restarted._current_input_fingerprint(claimed) == claimed.input_fingerprint
