from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.media import ProjectMediaSpec, parse_ffprobe
from cadscene.projects.models import ClipDefinition, StateReference, register_analysis_revision
from cadscene.projects.queue import LocalResourceQueue
from cadscene.projects.render_adapters import RenderAdapterRegistry, RenderExecutionPlan
from cadscene.projects.service import ProjectService
from cadscene.projects.service import _render_identity_payload
from cadscene.projects.service import _render_physical_inputs
import cadscene.projects.service as service_module
from cadscene.projects.workflow_adapters import default_workflow_adapters


class FakeRenderAdapter:
    workflow = "sfm_only"
    name = "fake-render"
    version = "1"

    def __init__(self) -> None:
        self.prepared_inputs = []

    def prepare(self, inputs):
        self.prepared_inputs.append(inputs)
        return RenderExecutionPlan(
            commands=(("fake-render", str(inputs.physical_video_path)),),
            validate=lambda: AdapterResult.failed("test validator not configured"),
        )


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


def _probe_rendered_video(_path: Path, *, pts: tuple[int, ...] = (0, 40)):
    return parse_ffprobe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "width": 1920,
                    "height": 1080,
                    "sample_aspect_ratio": "1:1",
                    "pix_fmt": "yuv420p",
                    "codec_name": "h264",
                    "profile": "High",
                    "time_base": "1/1000",
                    "avg_frame_rate": "25/1",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                    "color_primaries": "bt709",
                }
            ],
            "frames": [
                {
                    "media_type": "video",
                    "stream_index": 0,
                    "pts": value,
                    "pkt_duration": 40,
                }
                for value in pts
            ],
            "format": {"duration": str(len(pts) * 0.04)},
        }
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
    start_pts = len(clip_id) * 100
    frame_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "clips": [
                    {
                        "clip_id": clip_id,
                        "frames": [
                            {"ordinal": 0, "pts": start_pts, "duration_pts": 40},
                            {
                                "ordinal": 1,
                                "pts": start_pts + 40,
                                "duration_pts": 40,
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "source_start_pts": start_pts,
            "source_end_pts_exclusive": start_pts + 100,
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "interval_semantics": "half_open",
            "recommended_workflow": workflow,
            "needs_review": needs_review,
            "physical_mp4_path": str(physical),
            "frame_map_path": str(frame_map),
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
        media_probe=_probe_rendered_video,
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


def test_published_render_video_path_resolves_only_the_owned_revision(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _jobs = _system(tmp_path)
    target = (
        tmp_path
        / "projects"
        / "p1"
        / "render_outputs"
        / "ready"
        / "render-1"
    )
    target.mkdir(parents=True)
    video = target / "rendered.mp4"
    video.write_bytes(b"rendered")
    current = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "ready:render-1",
                    "clip_id": "ready",
                    "output_revision": "render-1",
                    "status": "success",
                    "outputs": {"video": str(video)},
                },
            ),
        ),
    )

    assert service.published_render_video_path("p1", "ready", "render-1") == video
    with pytest.raises(FileNotFoundError):
        service.published_render_video_path("p1", "ready", "render-other")


def test_published_render_video_path_keeps_stale_input_revision_previewable(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _jobs = _system(tmp_path)
    target = tmp_path / "projects" / "p1" / "render_outputs" / "ready" / "render-1"
    target.mkdir(parents=True)
    video = target / "rendered.mp4"
    video.write_bytes(b"rendered")
    current = repositories.render.load("p1")
    repositories.render.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clip_renders=(
                {
                    "render_id": "ready:render-1",
                    "clip_id": "ready",
                    "output_revision": "render-1",
                    "status": "stale_input",
                    "outputs": {"video": str(video)},
                },
            ),
        ),
    )

    assert service.published_render_video_path("p1", "ready", "render-1") == video


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


def test_render_reuses_validated_export_job_when_clip_has_no_embedded_paths(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    original = next(
        item for item in repositories.clips.load("p1").clips if item.clip_id == "ready"
    )
    analysis = dict(original.analysis)
    video = Path(str(analysis.pop("physical_mp4_path")))
    frame_map = Path(str(analysis.pop("frame_map_path")))
    logical_only = replace(original, analysis=analysis)
    project = repositories.project.load("p1")
    clips = repositories.clips.load("p1")
    export = service._new_export_job(
        "p1",
        logical_only,
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=clips.revision,
    )
    export = replace(
        export,
        status="success",
        stage="success",
        output_revision="clip-export-1",
        output_fingerprint="e" * 64,
        output_validated=True,
        validated_input_fingerprint=export.input_fingerprint,
        published_outputs={
            "video:ready": str(video),
            "frame_map:ready": str(frame_map),
        },
    )

    assert _render_physical_inputs(logical_only, (export,)) == (video, frame_map)


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


def test_render_preflight_skips_bad_clip_media_without_blocking_valid_clip(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories = _system(tmp_path)
    review = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "review"
    )
    Path(review.analysis["frame_map_path"]).write_text("not-json", encoding="utf-8")

    result = service.preflight_render_jobs(
        "p1", clip_ids=("ready", "review")
    )

    assert result.eligible == ("ready",)
    assert result.skipped == ("review",)
    assert "physical" in result.reasons["review"]
    enqueued = service.enqueue_render_jobs(
        "p1", clip_ids=("ready", "review")
    )
    assert enqueued.enqueued_clip_ids == ("ready",)


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

    assert payload["workbench"]["output_operation_id"] == "save-ready"


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
        media_probe=_probe_rendered_video,
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
        media_probe=_probe_rendered_video,
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
        "physical_video",
        "frame_map",
        "missing_video",
        "missing_map",
        "corrupt_map",
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
    elif change == "physical_video":
        clip = next(item for item in repositories.clips.load("p1").clips if item.clip_id == "ready")
        Path(clip.analysis["physical_mp4_path"]).write_bytes(b"changed-clip")
    elif change == "frame_map":
        clip = next(item for item in repositories.clips.load("p1").clips if item.clip_id == "ready")
        path = Path(clip.analysis["frame_map_path"])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["source_time_base"] = {"numerator": 1, "denominator": 999}
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif change in {"missing_video", "missing_map", "corrupt_map"}:
        clip = next(
            item
            for item in repositories.clips.load("p1").clips
            if item.clip_id == "ready"
        )
        key = "physical_mp4_path" if change == "missing_video" else "frame_map_path"
        path = Path(clip.analysis[key])
        if change == "corrupt_map":
            path.write_text("not-json", encoding="utf-8")
        else:
            path.unlink()

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:03Z",
        render_adapters=RenderAdapterRegistry((adapter,)),
        media_probe=_probe_rendered_video,
    )

    restored = rebuilt.restore_jobs("p1")

    assert restored.get(render_id).status == "superseded"


def test_prepare_clip_render_builds_manifest_free_immutable_adapter_inputs(
    tmp_path: Path,
) -> None:
    service, repositories, queue, adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    jobs_before = repositories.jobs.load("p1")
    render_before = repositories.render.load("p1")
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]

    plan = service.prepare_job_execution(
        "p1",
        render_id,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert plan.commands[0][0] == "fake-render"
    inputs = adapter.prepared_inputs[-1]
    assert inputs.project_id == "p1"
    assert inputs.clip_id == "ready"
    assert inputs.workflow == "sfm_only"
    assert inputs.attempt_directory == Path(lease.directory).resolve()
    assert [frame.pts for frame in inputs.authoritative_source_frames] == [
        500,
        540,
    ]
    assert inputs.source_time_base == Fraction(1, 1000)
    assert inputs.workbench_artifact_path.name == "camera_track.json"
    assert not hasattr(inputs, "repositories")
    assert repositories.jobs.load("p1") == jobs_before
    assert repositories.render.load("p1") == render_before


def _validated_render_result(service: ProjectService, job) -> AdapterResult:
    attempt = Path(job.attempts[-1].directory)
    video = attempt / "rendered.mp4"
    video.write_bytes(b"validated-rendered-video")
    frame_map = attempt / "render_frame_map.json"
    source_start = 500
    frame_map_payload = {
        "schema_version": 1,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "frames": [
            {
                "output_frame_ordinal": 0,
                "source_decoded_frame_ordinal": 0,
                "source_pts": source_start,
            },
            {
                "output_frame_ordinal": 1,
                "source_decoded_frame_ordinal": 1,
                "source_pts": source_start + 40,
            },
        ],
    }
    frame_map.write_text(json.dumps(frame_map_payload), encoding="utf-8")
    project = service.repositories.project.load(job.project_id)
    proof = {
        "rendered_frame_count": 2,
        "output_pts": [0, 40],
        "source_ordinals": [0, 1],
        "source_pts": [500, 540],
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "project_media_spec": project.media_spec,
        "video_sha256": sha256(video.read_bytes()).hexdigest(),
        "frame_map_sha256": sha256(frame_map.read_bytes()).hexdigest(),
    }
    output_fingerprint = sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AdapterResult(
        status="success",
        output_revision="render-output-ready-1",
        output_fingerprint=output_fingerprint,
        outputs={"video": str(video), "frame_map": str(frame_map)},
        validation_proof=proof,
    )


def test_finish_clip_render_validates_and_publishes_jobs_and_render_together(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)

    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "success", finished.error
    stored_job = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == render_id
    )
    render = repositories.render.load("p1").clip_renders[-1]
    assert stored_job["publication_operation_id"] == render["operation_id"]
    assert stored_job["operation_id"] == render["operation_id"]
    assert stored_job["validation_proof"] == result.validation_proof
    assert render["status"] == "success"
    assert render["workflow"] == "sfm_only"
    assert render["validation_proof"] == result.validation_proof
    assert Path(render["outputs"]["video"]).is_file()
    assert Path(render["outputs"]["frame_map"]).is_file()
    assert Path(render["outputs"]["video"]).parent != Path(lease.directory)
    assert (Path(lease.directory) / "rendered.mp4").is_file()


def test_finish_clip_render_recovers_when_publication_is_durable_before_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    publish = service_module.publish_manifests

    def publish_then_interrupt(*args, **kwargs):
        publish(*args, **kwargs)
        raise OSError("simulated interruption after durable publication")

    monkeypatch.setattr(service_module, "publish_manifests", publish_then_interrupt)

    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "success"
    assert queue.get(render_id).status == "success"
    assert len(repositories.render.load("p1").clip_renders) == 1


def test_render_publication_recovery_revalidates_durable_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    publish = service_module.publish_manifests

    def publish_tamper_then_interrupt(*args, **kwargs):
        publish(*args, **kwargs)
        target = (
            service.projects_root
            / "p1"
            / "render_outputs"
            / "ready"
            / str(result.output_revision)
            / "rendered.mp4"
        )
        target.write_bytes(b"tampered-after-durable-publication")
        monkeypatch.setattr(service_module, "publish_manifests", publish)
        raise OSError("simulated interruption after tampering durable output")

    monkeypatch.setattr(
        service_module, "publish_manifests", publish_tamper_then_interrupt
    )
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "failed"
    assert queue.get(render_id).status == "failed"
    recovered_job = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == render_id
    )
    render_record = repositories.render.load("p1").clip_renders[-1]
    assert render_record["job_id"] == render_id
    assert render_record["status"] == "failed_validation"
    assert render_record["operation_id"] == recovered_job["operation_id"]


def test_render_publication_recovery_downgrades_durable_stale_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    clip = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "ready"
    )
    publish = service_module.publish_manifests

    def publish_change_input_then_interrupt(*args, **kwargs):
        publish(*args, **kwargs)
        Path(clip.analysis["physical_mp4_path"]).write_bytes(
            b"changed-after-durable-publication"
        )
        monkeypatch.setattr(service_module, "publish_manifests", publish)
        raise OSError("simulated interruption after input changed")

    monkeypatch.setattr(
        service_module, "publish_manifests", publish_change_input_then_interrupt
    )
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    stored_job = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == render_id
    )
    render_record = repositories.render.load("p1").clip_renders[-1]
    assert finished.status == "superseded"
    assert stored_job["status"] == "superseded"
    assert render_record["status"] == "stale_input"
    assert stored_job["operation_id"] == render_record["operation_id"]


def test_render_publication_recovery_downgrades_stale_jobs_only_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    clip = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "ready"
    )
    publish_jobs = repositories.jobs._publish_prepared_unchecked
    publish_render = repositories.render._publish_prepared_unchecked
    changed_once = False
    failed_once = False

    def publish_jobs_then_change_input(*args, **kwargs):
        nonlocal changed_once
        published = publish_jobs(*args, **kwargs)
        if not changed_once:
            changed_once = True
            Path(clip.analysis["physical_mp4_path"]).write_bytes(
                b"changed-after-jobs-prefix"
            )
        return published

    def fail_first_render_publication(*args, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("simulated render owner publication crash")
        return publish_render(*args, **kwargs)

    monkeypatch.setattr(
        repositories.jobs,
        "_publish_prepared_unchecked",
        publish_jobs_then_change_input,
    )
    monkeypatch.setattr(
        repositories.render,
        "_publish_prepared_unchecked",
        fail_first_render_publication,
    )
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    stored_job = next(
        item
        for item in repositories.jobs.load("p1").jobs
        if item["job_id"] == render_id
    )
    render_record = repositories.render.load("p1").clip_renders[-1]
    assert finished.status == "superseded"
    assert stored_job["status"] == "superseded"
    assert render_record["status"] == "stale_input"
    assert stored_job["operation_id"] == render_record["operation_id"]


def test_finish_clip_render_changed_input_is_stale_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    project = repositories.project.load("p1")
    service.set_project_media_spec(
        "p1",
        replace(_media_spec(), width=1280, height=720),
        media_spec_revision="media-spec-new",
        expected_revision=project.revision,
    )

    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "stale_input"
    assert repositories.render.load("p1").clip_renders == ()
    assert (Path(lease.directory) / "rendered.mp4").is_file()


def test_finish_clip_render_rejects_missing_structured_validation_proof(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    invalid = AdapterResult.success(
        output_revision="unvalidated",
        output_fingerprint="f" * 64,
        outputs={"video": str(Path(lease.directory) / "rendered.mp4")},
    )

    finished = service.finish_job(
        "p1",
        render_id,
        invalid,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "failed"
    assert repositories.render.load("p1").clip_renders == ()


def test_finish_clip_render_uses_server_probe_not_adapter_frame_claim(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    service.media_probe = lambda path: _probe_rendered_video(path, pts=(0,))
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    dishonest = replace(
        result,
        validation_proof={**dict(result.validation_proof or {}), "output_pts": [0]},
    )

    finished = service.finish_job(
        "p1",
        render_id,
        dishonest,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "failed"
    assert repositories.render.load("p1").clip_renders == ()


def test_finish_rechecks_current_render_input_after_server_probe(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    clip = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "ready"
    )
    physical_video = Path(clip.analysis["physical_mp4_path"])

    def probe_and_change_input(path: Path):
        physical_video.write_bytes(b"changed-during-server-probe")
        return _probe_rendered_video(path)

    service.media_probe = probe_and_change_input
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "stale_input"
    assert repositories.render.load("p1").clip_renders == ()


def test_finish_rechecks_input_inside_cross_manifest_candidate_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    clip = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "ready"
    )
    physical_video = Path(clip.analysis["physical_mp4_path"])
    prepare = queue.prepare_success_candidate

    def prepare_then_change_input(*args, **kwargs):
        candidate = prepare(*args, **kwargs)
        physical_video.write_bytes(b"changed-after-success-candidate")
        return candidate

    monkeypatch.setattr(queue, "prepare_success_candidate", prepare_then_change_input)
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    assert finished.status == "stale_input"
    assert repositories.render.load("p1").clip_renders == ()


def test_attempt_media_changed_after_validation_is_not_published(
    tmp_path: Path,
) -> None:
    service, _repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    result = _validated_render_result(service, claimed)
    validated = service._validate_render_result(claimed, result)
    validated.video_path.write_bytes(b"changed-after-validation")

    with pytest.raises(ValueError, match="staged render"):
        service._publish_render_artifacts(claimed, validated)
    assert not (
        service.projects_root
        / "p1"
        / "render_outputs"
        / "ready"
        / str(result.output_revision)
    ).exists()


def test_cancel_clip_render_preserves_attempt_and_publishes_no_render(
    tmp_path: Path,
) -> None:
    service, repositories, _queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    attempt = Path(service.queue.get(render_id).attempts[-1].directory)
    diagnostic = attempt / "partial.log"
    diagnostic.write_text("partial", encoding="utf-8")

    cancelled = service.cancel_job("p1", render_id)

    assert cancelled.status == "cancelled"
    assert diagnostic.read_text(encoding="utf-8") == "partial"
    assert repositories.render.load("p1").clip_renders == ()


def _publish_successful_render(
    service: ProjectService,
    repositories,
    queue: LocalResourceQueue,
    render_id: str,
):
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    finished = service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    assert finished.status == "success"
    target = (
        service.projects_root
        / "p1"
        / "render_outputs"
        / "ready"
        / str(result.output_revision)
    )
    return claimed, result, target


@pytest.mark.parametrize("damage", ["video_content", "missing_map", "map_directory"])
def test_republication_rejects_tampered_or_incomplete_immutable_render_revision(
    tmp_path: Path,
    damage: str,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed, result, target = _publish_successful_render(
        service, repositories, queue, render_id
    )
    render_before = repositories.render.load("p1")
    if damage == "video_content":
        (target / "rendered.mp4").write_bytes(b"tampered")
    else:
        frame_map = target / "render_frame_map.json"
        frame_map.unlink()
        if damage == "map_directory":
            frame_map.mkdir()

    with pytest.raises(ValueError, match="immutable render revision"):
        service._publish_render_artifacts(
            claimed, service._validate_render_result(claimed, result)
        )
    assert repositories.render.load("p1") == render_before
    assert len(render_before.clip_renders) == 1
    assert render_before.clip_renders[0]["job_id"] == render_id


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("project_id", "other-project"),
        ("clip_id", "other-clip"),
        ("input_revision", "other-input"),
        ("adapter_name", "other-adapter"),
        ("adapter_version", "999"),
        ("output_revision", "other-output"),
    ],
)
def test_republication_rejects_immutable_render_revision_identity_collision(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed, result, target = _publish_successful_render(
        service, repositories, queue, render_id
    )
    render_before = repositories.render.load("p1")
    manifest_path = target / "render_output_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = replacement
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="immutable render revision"):
        service._publish_render_artifacts(
            claimed, service._validate_render_result(claimed, result)
        )
    assert repositories.render.load("p1") == render_before


def test_published_render_is_idempotent_and_restores_only_with_exact_proof(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )

    reused = service._publish_render_artifacts(
        claimed, service._validate_render_result(claimed, result)
    )
    assert Path(reused["video"]).read_bytes() == b"validated-rendered-video"

    repeated = service.enqueue_render_jobs("p1", clip_ids=("ready",))
    assert repeated.job_ids == (render_id,)
    assert len(repositories.render.load("p1").clip_renders) == 1

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:04Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )
    restored = rebuilt.restore_jobs("p1")
    assert restored.get(render_id).status == "success"
    assert restored.get(render_id).validation_proof == result.validation_proof


def test_restore_keeps_render_current_after_only_workbench_session_state_changes(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            operation_id="session-close",
            clips=tuple(
                replace(
                    clip,
                    operation_id="session-close",
                    references=tuple(
                        replace(
                            reference,
                            operation_id="session-close",
                            value={
                                **reference.value,
                                "status": "saved",
                                "workbench_output_operation_id": "save-ready",
                            },
                        )
                        if reference.key == "workbench:ready"
                        else reference
                        for reference in clip.references
                    ),
                )
                if clip.clip_id == "ready"
                else clip
                for clip in value.clips
            ),
        ),
    )
    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:05Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )

    restored = rebuilt.restore_jobs("p1")

    assert restored.get(render_id).status == "success"
    assert repositories.render.load("p1").clip_renders[-1]["status"] == "success"


def test_restore_does_not_trust_tampered_successful_render_publication(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    published = repositories.render.load("p1").clip_renders[-1]
    Path(published["outputs"]["video"]).write_bytes(b"tampered")

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:05Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )
    restored = rebuilt.restore_jobs("p1")

    restored_job = restored.get(render_id)
    render_record = repositories.render.load("p1").clip_renders[-1]
    assert restored_job.status == "failed"
    assert render_record["status"] == "failed_validation"
    assert restored_job.operation_id == render_record["operation_id"]
    assert restored_job.published_outputs


def test_restore_downgrades_changed_render_input_and_owner_together(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    clip = next(
        item
        for item in repositories.clips.load("p1").clips
        if item.clip_id == "ready"
    )
    Path(clip.analysis["physical_mp4_path"]).write_bytes(b"changed-input")

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:05Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )
    restored = rebuilt.restore_jobs("p1")

    restored_job = restored.get(render_id)
    render_record = repositories.render.load("p1").clip_renders[-1]
    assert restored_job.status == "superseded"
    assert render_record["status"] == "stale_input"
    assert restored_job.operation_id == render_record["operation_id"]
    assert restored_job.published_outputs


@pytest.mark.parametrize("damage", ["missing", "operation", "job_id", "extra"])
def test_restore_requires_unique_exact_render_owner_record(
    tmp_path: Path,
    damage: str,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    current = repositories.render.load("p1")
    record = dict(current.clip_renders[-1])
    if damage == "missing":
        records = ()
    elif damage == "operation":
        records = ({**record, "operation_id": "wrong-operation"},)
    elif damage == "extra":
        records = ({**record, "unexpected": "field"},)
    else:
        records = ({**record, "job_id": "wrong-job"},)
    repositories.render.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(value, clip_renders=records),
    )

    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:06Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )
    restored = rebuilt.restore_jobs("p1")

    restored_job = restored.get(render_id)
    assert restored_job.status != "success"
    render_manifest = repositories.render.load("p1")
    assert render_manifest.operation_id == restored_job.operation_id
    if render_manifest.clip_renders:
        assert render_manifest.clip_renders[-1]["status"] != "success"
        assert (
            render_manifest.clip_renders[-1]["operation_id"]
            == restored_job.operation_id
        )


def test_persisted_render_rejects_unsafe_output_revision_before_path_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    persisted = queue.get(render_id)
    unsafe_revision = "../outside"
    target = (
        service.projects_root
        / "p1"
        / "render_outputs"
        / "ready"
        / unsafe_revision
    )
    polluted = replace(
        persisted,
        output_revision=unsafe_revision,
        published_outputs={
            "video": str(target / "rendered.mp4"),
            "frame_map": str(target / "render_frame_map.json"),
            "manifest": str(target / "render_output_manifest.json"),
        },
    )
    monkeypatch.setattr(
        repositories.render,
        "load",
        lambda _project_id: (_ for _ in ()).throw(
            AssertionError("unsafe revision reached render repository lookup")
        ),
    )

    assert service._validate_persisted_render_success(polluted) is False


def test_restore_missing_clip_fails_old_render_without_blocking_project(
    tmp_path: Path,
) -> None:
    service, repositories, queue, _adapter, _trajectories, render_id = (
        _enqueue_ready_render(tmp_path)
    )
    claimed = queue.claim_next_unstarted()
    assert claimed is not None and claimed.job_id == render_id
    lease = claimed.attempts[-1]
    result = _validated_render_result(service, claimed)
    service.finish_job(
        "p1",
        render_id,
        result,
        attempt_number=lease.number,
        claim_token=str(lease.worker_claim_token),
    )
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=tuple(item for item in value.clips if item.clip_id != "ready"),
        ),
    )
    rebuilt = ProjectService(
        repositories,
        LocalResourceQueue(),
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "2026-08-04T00:00:07Z",
        render_adapters=RenderAdapterRegistry((FakeRenderAdapter(),)),
        media_probe=_probe_rendered_video,
    )

    restored = rebuilt.restore_jobs("p1")

    assert restored.get(render_id).status != "success"


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
