from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading

from cadscene.projects.analysis import ProjectAnalysisCoordinator
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.uploads import PublishedUpload


def _upload(project_id: str, kind: str, path: Path) -> PublishedUpload:
    report = path.with_suffix(path.suffix + ".validation.json")
    report.write_text("{}", encoding="utf-8")
    return PublishedUpload(
        project_id=project_id,
        asset_type=kind,
        original_filename=path.name,
        path=path,
        size_bytes=path.stat().st_size,
        sha256="a" * 64,
        validation={},
        validation_report_path=report,
    )


def _fake_video_analyzer(**kwargs):
    revision = kwargs["analysis_revision"]
    output = Path(kwargs["output_root"]) / "02_video_analysis" / revision
    output.mkdir(parents=True)
    (output / "clip_manifest.json").write_text(
        json.dumps(
            {
                "analysis_revision": revision,
                "clips": [
                    {
                        "project_id": kwargs["project_id"],
                        "clip_id": "clip-0001",
                        "analysis_revision": revision,
                        "source_start_pts": 0,
                        "source_end_pts_exclusive": 100,
                        "source_time_base": {"numerator": 1, "denominator": 25},
                        "interval_semantics": "half_open",
                        "scene_index": 1,
                        "segment_index": 1,
                        "recommended_workflow": "sfm_only",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return output


def test_published_video_runs_existing_analysis_and_activates_initial_clips(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value, source_assets={"video": {"path": str(video)}}
        ),
    )
    coordinator = ProjectAnalysisCoordinator(
        repositories,
        projects_root=tmp_path / "projects",
        storage_root=tmp_path,
        now=lambda: "later",
        identity=lambda: "analysis-1",
        video_analyzer=_fake_video_analyzer,
    )

    coordinator.trigger("p1", "video", _upload("p1", "video", video)).result()

    project = repositories.project.load("p1")
    clips = repositories.clips.load("p1")
    assert project.active_analysis_revision == "analysis-1"
    assert project.project_state == "ready"
    assert clips.analysis_revision == "analysis-1"
    assert clips.clips[0].generated_display_name == "场景 01 · 第 1 段"
    assert (
        tmp_path
        / "projects"
        / "p1"
        / "analyses"
        / "analysis-1"
        / "02_video_analysis"
        / "clip_manifest.json"
    ).is_file()


def test_reanalysis_creates_candidate_without_overwriting_active_user_layers(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value, source_assets={"video": {"path": str(video)}}
        ),
    )
    revisions = iter(("analysis-1", "analysis-2"))
    coordinator = ProjectAnalysisCoordinator(
        repositories,
        projects_root=tmp_path / "projects",
        storage_root=tmp_path,
        now=lambda: "later",
        identity=lambda: next(revisions),
        video_analyzer=_fake_video_analyzer,
    )
    uploaded = _upload("p1", "video", video)
    coordinator.trigger("p1", "video", uploaded).result()
    current = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=(
                value.clips[0]
                .with_custom_display_name("东侧入口")
                .with_workflow_override("pure_rotation"),
            ),
        ),
    )

    coordinator.trigger("p1", "srt", uploaded).result()

    project = repositories.project.load("p1")
    active = repositories.clips.load("p1").clips[0]
    assert project.active_analysis_revision == "analysis-1"
    assert project.candidate_analysis_revision == "analysis-2"
    assert active.analysis_revision == "analysis-1"
    assert active.display_name == "东侧入口"
    assert active.workflow_override == "pure_rotation"


def test_changed_analysis_request_does_not_publish_old_input_result(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(video)},
                "_analysis": {"request_key": "old", "status": "queued"},
            },
        ),
    )

    def analyzer_that_changes_input(**kwargs):
        output = _fake_video_analyzer(**kwargs)
        current = repositories.project.load("p1")
        repositories.project.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                source_assets={
                    **value.source_assets,
                    "_analysis": {"request_key": "new", "status": "queued"},
                },
            ),
        )
        return output

    coordinator = ProjectAnalysisCoordinator(
        repositories,
        projects_root=tmp_path / "projects",
        storage_root=tmp_path,
        now=lambda: "later",
        identity=lambda: "analysis-old",
        video_analyzer=analyzer_that_changes_input,
    )

    coordinator.trigger("p1", "video", _upload("p1", "video", video)).result()

    assert repositories.project.load("p1").active_analysis_revision is None
    assert repositories.clips.load("p1").clips == ()


def test_changed_analysis_request_does_not_attach_old_cad_result_to_new_asset(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    video = tmp_path / "video.mp4"
    cad = tmp_path / "design.dxf"
    video.write_bytes(b"video")
    cad.write_bytes(b"cad")
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(video)},
                "cad": {"path": str(cad), "sha256": "old"},
                "_analysis": {"request_key": "old", "status": "queued"},
            },
        ),
    )

    def importer_that_changes_input(*_args, **_kwargs):
        current = repositories.project.load("p1")
        repositories.project.update(
            "p1",
            expected_revision=current.revision,
            mutate=lambda value: replace(
                value,
                source_assets={
                    **value.source_assets,
                    "cad": {"path": str(cad), "sha256": "new"},
                    "_analysis": {"request_key": "new", "status": "queued"},
                },
            ),
        )
        return {"cad": {"entities": 3}}

    coordinator = ProjectAnalysisCoordinator(
        repositories,
        projects_root=tmp_path / "projects",
        storage_root=tmp_path,
        now=lambda: "later",
        cad_importer=importer_that_changes_input,
        video_analyzer=_fake_video_analyzer,
    )

    coordinator.trigger("p1", "cad", _upload("p1", "cad", cad)).result()

    current_cad = repositories.project.load("p1").source_assets["cad"]
    assert current_cad["sha256"] == "new"
    assert "analysis" not in current_cad


def test_analysis_uses_asset_snapshot_captured_when_triggered(
    tmp_path: Path,
) -> None:
    repositories = project_repositories(tmp_path / "projects")
    repositories.create_project("p1", updated_at="now")
    old_video = tmp_path / "old.mp4"
    new_video = tmp_path / "new.mp4"
    old_video.write_bytes(b"old")
    new_video.write_bytes(b"new")
    current = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(old_video), "sha256": "old"},
                "_analysis": {"request_key": "request-old", "status": "queued"},
            },
        ),
    )
    observed: list[Path] = []

    def analyzer(**kwargs):
        observed.append(Path(kwargs["video_path"]))
        return _fake_video_analyzer(**kwargs)

    gate = threading.Event()
    coordinator = ProjectAnalysisCoordinator(
        repositories,
        projects_root=tmp_path / "projects",
        storage_root=tmp_path,
        now=lambda: "later",
        identity=lambda: "analysis-old",
        video_analyzer=analyzer,
    )
    coordinator._executor.submit(gate.wait)
    future = coordinator.trigger("p1", "video", _upload("p1", "video", old_video))
    changed = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=changed.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                "video": {"path": str(new_video), "sha256": "new"},
                "_analysis": {"request_key": "request-new", "status": "queued"},
            },
        ),
    )
    gate.set()
    future.result()
    coordinator.close()

    assert observed == []  # old request is superseded before any input is opened
    assert repositories.project.load("p1").active_analysis_revision is None
