from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.media import ProjectMediaSpec
from cadscene.projects.models import ClipDefinition, StateReference, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.render_adapters import RenderAdapterRegistry
from cadscene.projects.service import ProjectService
from cadscene.projects.service import _render_identity_payload
from cadscene.projects.workflow_adapters import default_workflow_adapters


class FakeRenderAdapter:
    workflow = "sfm_only"
    name = "fake-render"
    version = "1"

    def prepare(self, inputs):  # pragma: no cover - execution is out of substage scope
        raise AssertionError("enqueue must not prepare or execute the adapter")


def _media_spec() -> ProjectMediaSpec:
    return ProjectMediaSpec(
        width=1920,
        height=1080,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 1000),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=Fraction(25, 1),
    )


def _clip(
    tmp_path: Path,
    clip_id: str,
    *,
    workflow: str = "sfm_only",
    needs_review: bool = False,
) -> ClipDefinition:
    physical = tmp_path / f"{clip_id}.mp4"
    physical.write_bytes(b"clip")
    frame_map = tmp_path / f"{clip_id}-frame-map.json"
    frame_map.write_text("{}", encoding="utf-8")
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "source_start_pts": len(clip_id) * 100,
            "source_end_pts_exclusive": len(clip_id) * 100 + 100,
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "interval_semantics": "half_open",
            "recommended_workflow": workflow,
            "needs_review": needs_review,
            "physical_mp4_path": str(physical),
            "clip_frame_map_path": str(frame_map),
            "input_snapshot": {
                "request_key": "analysis-request-1",
                "video": {"path": str(tmp_path / "source.mp4"), "sha256": "a" * 64},
                "cad": None,
                "srt": None,
                "analysis_artifact": {
                    "path": str(tmp_path / "analysis.json"),
                    "artifact_id": "analysis-artifact-1",
                },
            },
        }
    )


def _write_workbench_output(
    projects_root: Path,
    clip_id: str,
    workflow: str,
    operation_id: str,
) -> tuple[str, str]:
    revision = f"workbench-{clip_id}"
    target = projects_root / "p1" / "workbench_outputs" / revision
    artifacts = target / "artifacts"
    artifacts.mkdir(parents=True)
    artifact = artifacts / "camera_track.json"
    artifact.write_bytes(f"track:{clip_id}".encode("utf-8"))
    artifact_hash = sha256(artifact.read_bytes()).hexdigest()
    payload = {
        "schema_version": "1.0",
        "project_id": "p1",
        "clip_id": clip_id,
        "workflow": workflow,
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
    canonical = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fingerprint = sha256(canonical).hexdigest()
    payload["workbench_output_fingerprint"] = fingerprint
    (target / "workbench_output_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return revision, fingerprint


def _system(tmp_path: Path):
    projects_root = tmp_path / "projects"
    repositories = project_repositories(projects_root)
    repositories.create_project("p1", updated_at="2026-08-04T00:00:00Z")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            register_analysis_revision(
                value, "analysis-1", operation_id="analysis-operation"
            ),
            source_assets={"video_path": str(source)},
        ),
    )
    clips = (
        _clip(tmp_path, "ready"),
        _clip(tmp_path, "review", needs_review=True),
        _clip(tmp_path, "unsaved"),
        _clip(tmp_path, "stale"),
        _clip(tmp_path, "unsupported", workflow="pure_rotation"),
    )
    clips_manifest = repositories.clips.load("p1")
    clips_manifest = repositories.clips.update(
        "p1",
        expected_revision=clips_manifest.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=clips
        ),
    )
    queue = LocalResourceQueue()
    render_adapter = FakeRenderAdapter()
    service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: "2026-08-04T00:00:01Z",
        render_adapters=RenderAdapterRegistry((render_adapter,)),
    )
    project_for_spec = repositories.project.load("p1")
    service.set_project_media_spec(
        "p1",
        _media_spec(),
        media_spec_revision="media-spec-1",
        expected_revision=project_for_spec.revision,
    )
    project = repositories.project.load("p1")
    trajectories = []
    references: dict[str, StateReference] = {}
    for clip in clips:
        adapter = default_workflow_adapters().for_workflow(str(clip.resolved_workflow))
        job = service._new_job(
            "p1",
            clip,
            job_type="trajectory",
            resource_class="heavy_compute",
            adapter_name=adapter.name,
            adapter_version=adapter.version,
            exclusive_key=f"trajectory:p1:{clip.clip_id}",
            dependency_ids=(),
            project_assets=project.source_assets,
            project_revision=project.revision,
            clips_revision=clips_manifest.revision,
        )
        trajectory_path = Path(job.attempts[-1].directory) / "camera_trajectory.json"
        trajectory_path.parent.mkdir(parents=True)
        trajectory_path.write_text("{}", encoding="utf-8")
        job = replace(
            job,
            status=("stale_input" if clip.clip_id == "stale" else "success"),
            stage=("stale_input" if clip.clip_id == "stale" else "success"),
            output_revision=f"trajectory-{clip.clip_id}",
            output_fingerprint=sha256(trajectory_path.read_bytes()).hexdigest(),
            output_validated=clip.clip_id != "stale",
            validated_input_fingerprint=(
                None if clip.clip_id == "stale" else job.input_fingerprint
            ),
            published_outputs={"trajectory": str(trajectory_path)},
        )
        queue.submit(job)
        trajectories.append(job)
        if clip.clip_id != "unsaved":
            revision, fingerprint = _write_workbench_output(
                projects_root,
                clip.clip_id,
                str(clip.resolved_workflow),
                f"save-{clip.clip_id}",
            )
            references[clip.clip_id] = StateReference(
                owner="clips",
                key=f"workbench:{clip.clip_id}",
                operation_id=f"save-{clip.clip_id}",
                value={
                    "status": "saved",
                    "workflow": clip.resolved_workflow,
                    "input_revision": clip.analysis_revision,
                    "input_fingerprint": job.input_fingerprint,
                    "trajectory_job_id": job.job_id,
                    "trajectory_output_revision": job.output_revision,
                    "trajectory_output_fingerprint": job.output_fingerprint,
                    "workbench_output_revision": revision,
                    "workbench_output_fingerprint": fingerprint,
                },
            )
    jobs_manifest = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=jobs_manifest.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(job.to_dict() for job in trajectories),
            queue_order=tuple(job.job_id for job in trajectories),
        ),
    )
    current_clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current_clips.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(
                replace(clip, references=(references[clip.clip_id],))
                if clip.clip_id in references
                else clip
                for clip in value.clips
            ),
        ),
    )
    return service, repositories, queue, render_adapter, trajectories


def test_render_preflight_requires_current_trajectory_and_saved_workbench_proof(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    render_before = repositories.render.load("p1")

    result = service.preflight_render_jobs("p1")

    assert result.eligible == ("ready",)
    assert result.confirmation_required == ("review",)
    assert result.skipped == ("unsaved", "stale", "unsupported")
    assert "saved workbench" in result.reasons["unsaved"]
    assert "trajectory" in result.reasons["stale"]
    assert "render adapter" in result.reasons["unsupported"]
    assert repositories.render.load("p1") == render_before


def test_preflight_rejects_tampered_workbench_artifact(tmp_path: Path) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    clip = next(item for item in repositories.clips.load("p1").clips if item.clip_id == "ready")
    reference = clip.references[0]
    artifact = (
        tmp_path
        / "projects"
        / "p1"
        / "workbench_outputs"
        / str(reference.value["workbench_output_revision"])
        / "artifacts"
        / "camera_track.json"
    )
    artifact.write_bytes(b"tampered")

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "immutable workbench" in result.reasons["ready"]


def test_preflight_requires_the_validated_trajectory_output_not_any_diagnostic(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, trajectories = _system(tmp_path)
    ready = next(job for job in trajectories if job.clip_id == "ready")
    current = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(
                {
                    **item,
                    "published_outputs": {
                        "diagnostic": ready.published_outputs["trajectory"]
                    },
                }
                if item["job_id"] == ready.job_id
                else item
                for item in value.jobs
            ),
        ),
    )

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "trajectory" in result.reasons["ready"]


def test_preflight_rejects_trajectory_file_tampered_after_validation(
    tmp_path: Path,
) -> None:
    service, _repositories, _queue, _adapter, trajectories = _system(tmp_path)
    ready = next(job for job in trajectories if job.clip_id == "ready")
    Path(ready.published_outputs["trajectory"]).write_bytes(b"tampered")

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "trajectory" in result.reasons["ready"]


def test_preflight_rejects_trajectory_artifact_outside_current_attempt(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, trajectories = _system(tmp_path)
    ready = next(job for job in trajectories if job.clip_id == "ready")
    outside = tmp_path / "outside-trajectory.json"
    outside.write_bytes(Path(ready.published_outputs["trajectory"]).read_bytes())
    current = repositories.jobs.load("p1")
    repositories.jobs.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            jobs=tuple(
                {
                    **item,
                    "published_outputs": {"trajectory": str(outside)},
                }
                if item["job_id"] == ready.job_id
                else item
                for item in value.jobs
            ),
        ),
    )

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "trajectory" in result.reasons["ready"]


def test_preflight_rejects_workbench_reference_trajectory_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(
                replace(
                    clip,
                    references=(
                        replace(
                            clip.references[0],
                            value={
                                **clip.references[0].value,
                                "trajectory_output_fingerprint": "f" * 64,
                            },
                        ),
                    ),
                )
                if clip.clip_id == "ready"
                else clip
                for clip in value.clips
            ),
        ),
    )

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "saved workbench" in result.reasons["ready"]


def test_preflight_rejects_workbench_manifest_operation_mismatch(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    clip = next(
        item for item in repositories.clips.load("p1").clips if item.clip_id == "ready"
    )
    revision = str(clip.references[0].value["workbench_output_revision"])
    manifest_path = (
        tmp_path
        / "projects"
        / "p1"
        / "workbench_outputs"
        / revision
        / "workbench_output_manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["operation_id"] = "different-save-operation"
    unhashed = dict(payload)
    unhashed.pop("workbench_output_fingerprint")
    fingerprint = sha256(
        (json.dumps(unhashed, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    payload["workbench_output_fingerprint"] = fingerprint
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(
                replace(
                    item,
                    references=(
                        replace(
                            item.references[0],
                            value={
                                **item.references[0].value,
                                "workbench_output_fingerprint": fingerprint,
                            },
                        ),
                    ),
                )
                if item.clip_id == "ready"
                else item
                for item in value.clips
            ),
        ),
    )

    result = service.preflight_render_jobs("p1", clip_ids=("ready",))

    assert result.skipped == ("ready",)
    assert "immutable workbench" in result.reasons["ready"]


def test_render_identity_includes_saved_workbench_operation_id(tmp_path: Path) -> None:
    service, repositories, _queue, adapter, trajectories = _system(tmp_path)
    project = repositories.project.load("p1")
    clips = repositories.clips.load("p1")
    clip = next(item for item in clips.clips if item.clip_id == "ready")
    trajectory = next(item for item in trajectories if item.clip_id == "ready")

    payload = _render_identity_payload(
        clip=clip,
        project_revision=project.revision,
        clips_revision=clips.revision,
        trajectory=trajectory,
        workbench=clip.references[0],
        media_spec=ProjectMediaSpec.from_dict(project.media_spec),
        media_spec_revision=str(project.media_spec_revision),
        adapter_name=adapter.name,
        adapter_version=adapter.version,
    )

    assert payload["workbench"]["operation_id"] == "save-ready"


def test_project_media_specs_survive_service_rebuild_and_remain_project_scoped(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    repositories.create_project("p2", updated_at="2026-08-04T00:00:00Z")
    p2 = repositories.project.load("p2")
    service.set_project_media_spec(
        "p2",
        replace(_media_spec(), width=1280, height=720),
        media_spec_revision="media-spec-p2",
        expected_revision=p2.revision,
    )

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:02Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
    )
    rebuilt.restore_jobs("p1")
    rebuilt.restore_jobs("p2")

    p1_restored = repositories.project.load("p1")
    p2_restored = repositories.project.load("p2")
    assert ProjectMediaSpec.from_dict(p1_restored.media_spec).width == 1920
    assert p1_restored.media_spec_revision == "media-spec-1"
    assert ProjectMediaSpec.from_dict(p2_restored.media_spec).width == 1280
    assert p2_restored.media_spec_revision == "media-spec-p2"
    assert rebuilt.preflight_render_jobs("p1", clip_ids=("ready",)).eligible == (
        "ready",
    )


def _enqueue_ready_render(tmp_path: Path):
    service, repositories, queue, adapter, trajectories = _system(tmp_path)
    result = service.enqueue_render_jobs("p1", clip_ids=("ready",))
    return service, repositories, queue, adapter, trajectories, result.job_ids[0]


def test_restore_keeps_current_render_input_and_only_interrupts_unstarted_run(
    tmp_path: Path,
) -> None:
    _service, repositories, _queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:03Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
    )

    restored = rebuilt.restore_jobs("p1")

    assert restored.get(render_id).status == "interrupted"


@pytest.mark.parametrize(
    "change",
    [
        "workflow",
        "workbench",
        "trajectory",
        "media_spec",
        "adapter_version",
        "trajectory_file",
    ],
)
def test_restore_supersedes_render_when_any_bound_input_changes(
    tmp_path: Path,
    change: str,
) -> None:
    service, repositories, _queue, _adapter, trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    adapter = FakeRenderAdapter()
    if change == "workflow":
        current = repositories.clips.load("p1")
        repositories.clips.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                clips=tuple(
                    clip.with_workflow_override("pure_rotation")
                    if clip.clip_id == "ready"
                    else clip
                    for clip in value.clips
                ),
            ),
        )
    elif change == "workbench":
        current = repositories.clips.load("p1")
        repositories.clips.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                clips=tuple(
                    replace(
                        clip,
                        references=(
                            replace(
                                clip.references[0],
                                value={
                                    **clip.references[0].value,
                                    "workbench_output_fingerprint": "e" * 64,
                                },
                            ),
                        ),
                    )
                    if clip.clip_id == "ready"
                    else clip
                    for clip in value.clips
                ),
            ),
        )
    elif change == "trajectory":
        dependency = next(job for job in trajectories if job.clip_id == "ready")
        current = repositories.jobs.load("p1")
        repositories.jobs.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                jobs=tuple(
                    {**item, "output_revision": "trajectory-replaced"}
                    if item["job_id"] == dependency.job_id
                    else item
                    for item in value.jobs
                ),
            ),
        )
    elif change == "media_spec":
        current = repositories.project.load("p1")
        service.set_project_media_spec(
            "p1",
            replace(_media_spec(), width=1280, height=720),
            media_spec_revision="media-spec-2",
            expected_revision=current.revision,
        )
    elif change == "adapter_version":
        adapter.version = "2"
    elif change == "trajectory_file":
        dependency = next(job for job in trajectories if job.clip_id == "ready")
        Path(dependency.published_outputs["trajectory"]).write_bytes(b"tampered")

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:03Z",
        render_adapters=RenderAdapterRegistry((adapter,)),
    )

    restored = rebuilt.restore_jobs("p1")

    assert restored.get(render_id).status == "superseded"


def test_enqueue_render_jobs_builds_media_dag_identity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    service, repositories, queue, adapter, trajectories = _system(tmp_path)
    render_before = repositories.render.load("p1")
    trajectory_by_clip = {job.clip_id: job for job in trajectories}

    first = service.enqueue_render_jobs(
        "p1",
        clip_ids=("ready", "review", "unsaved"),
        confirmed_clip_ids=("review",),
    )
    repeated = service.enqueue_render_jobs(
        "p1",
        clip_ids=("ready", "review", "unsaved"),
        confirmed_clip_ids=("review",),
    )

    assert first.enqueued_clip_ids == ("ready", "review")
    assert repeated.job_ids == first.job_ids
    jobs = [queue.get(job_id) for job_id in first.job_ids]
    assert [job.status for job in jobs] == ["running", "queued"]
    for job in jobs:
        assert job.job_type == "clip_render"
        assert job.resource_class == "media_io"
        assert job.depends_on_job_ids == (trajectory_by_clip[job.clip_id].job_id,)
        assert job.exclusive_key == f"render:p1:{job.clip_id}"
        assert job.adapter_name == "fake-render"
        assert job.adapter_version == "1"
        assert job.input_revision
        assert job.input_fingerprint
        assert job.idempotency_key
    stored = repositories.jobs.load("p1")
    assert sum(item["job_type"] == "clip_render" for item in stored.jobs) == 2
    assert repositories.render.load("p1") == render_before

    adapter.version = "2"
    changed = service.enqueue_render_jobs("p1", clip_ids=("ready",))
    changed_job = queue.get(changed.job_ids[0])
    assert changed_job.job_id != first.job_ids[0]
    assert changed_job.input_fingerprint != jobs[0].input_fingerprint
    assert changed_job.idempotency_key != jobs[0].idempotency_key
    assert changed_job.status == "queued"
