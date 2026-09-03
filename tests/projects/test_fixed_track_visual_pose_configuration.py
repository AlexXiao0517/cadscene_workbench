from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import pytest

from cadscene.projects.http_api import ProjectApi
from cadscene.projects.models import StateReference
from cadscene.projects.service import _fixed_track_visual_pose_adapter_parameters
from cadscene.projects.uploads import ValidatedUploadStore

from .test_full_pose_configuration import _configure_project_inputs
from .test_service_jobs import clip, service_with_clips


def _fixed_track_clip(*, trajectory_coverage: float = 1.0):
    base = clip("clip-1", workflow="srt_fixed_track_visual_pose")
    return replace(
        base,
        analysis={
            **base.analysis,
            "srt_coverage": {
                "kind": "partial" if trajectory_coverage >= 0.8 else "none",
                "trajectory_coverage": trajectory_coverage,
                "full_pose_coverage": 0.0,
                "overlapping_record_count": 3,
            },
        },
    )


def _confirmed_service(tmp_path: Path, *, trajectory_coverage: float = 1.0):
    service, repositories, queue = service_with_clips(
        tmp_path, (_fixed_track_clip(trajectory_coverage=trajectory_coverage),)
    )
    _configure_project_inputs(tmp_path, repositories)
    project = repositories.project.load("p1")
    service.confirm_cad_georeference(
        "p1",
        service.recommend_cad_georeference("p1")[0].to_dict(),
        expected_revision=project.revision,
    )
    return service, repositories, queue


def test_fixed_track_preflight_requires_fov_and_relative_height(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)

    missing_fov = service.preflight_trajectory_jobs("p1")

    assert missing_fov.skipped == ("clip-1",)
    assert "horizontal FOV" in missing_fov.reasons["clip-1"]

    clips = repositories.clips.load("p1")
    service.update_srt_fixed_track_visual_pose_settings(
        "p1",
        "clip-1",
        expected_revision=clips.revision,
        horizontal_fov_deg=72.0,
        route_offset_xyz_m=(2.0, -1.0, 5.0),
    )

    assert service.preflight_trajectory_jobs("p1").eligible == ("clip-1",)

    weak_service, weak_repositories, _queue = _confirmed_service(
        tmp_path / "weak", trajectory_coverage=0.3
    )
    weak_clips = weak_repositories.clips.load("p1")
    weak_service.update_srt_fixed_track_visual_pose_settings(
        "p1",
        "clip-1",
        expected_revision=weak_clips.revision,
        horizontal_fov_deg=72.0,
    )
    weak = weak_service.preflight_trajectory_jobs("p1")
    assert weak.skipped == ("clip-1",)
    assert "relative height" in weak.reasons["clip-1"]


def test_settings_store_one_xyz_offset_and_stale_old_references(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)
    current = repositories.clips.load("p1")
    selected = current.clips[0]
    repositories.clips.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            clips=(
                replace(
                    selected,
                    references=(
                        StateReference(
                            owner="jobs",
                            key="job:old",
                            operation_id="old-operation",
                            value={"status": "success", "reference_type": "trajectory"},
                        ),
                    ),
                ),
            ),
        ),
    )
    current = repositories.clips.load("p1")

    updated = service.update_srt_fixed_track_visual_pose_settings(
        "p1",
        "clip-1",
        expected_revision=current.revision,
        horizontal_fov_deg=72.0,
        route_offset_xyz_m=(2.0, -1.0, 5.0),
    )

    selected = updated.clips[0]
    assert selected.manual_definition["srt_fixed_track_visual_pose"] == {
        "schema_version": 1,
        "horizontal_fov_deg": 72.0,
        "route_offset_xyz_m": [2.0, -1.0, 5.0],
    }
    assert selected.references[0].value["status"] == "stale"
    assert selected.references[0].value["stale_reason"] == (
        "fixed_track_visual_pose_settings_changed"
    )


@pytest.mark.parametrize(
    "fov,offset",
    [
        (1.0, (0.0, 0.0, 0.0)),
        (179.0, (0.0, 0.0, 0.0)),
        (72.0, (0.0, math.nan, 0.0)),
        (72.0, (0.0, 0.0)),
    ],
)
def test_settings_reject_invalid_fov_or_xyz_offset(
    tmp_path: Path, fov, offset
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)

    with pytest.raises(ValueError):
        service.update_srt_fixed_track_visual_pose_settings(
            "p1",
            "clip-1",
            expected_revision=repositories.clips.load("p1").revision,
            horizontal_fov_deg=fov,
            route_offset_xyz_m=offset,
        )


def test_fixed_track_settings_api_and_snapshot_are_versioned(tmp_path: Path) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(tmp_path / "projects"),
        now=lambda: "2026-09-03T00:00:00Z",
    )

    response = api.handle(
        "PATCH",
        "/api/projects/p1/clips/clip-1/srt-fixed-track-visual-pose",
        json_body={
            "expected_revision": repositories.clips.load("p1").revision,
            "horizontal_fov_deg": 72.0,
            "route_offset_xyz_m": [2.0, -1.0, 5.0],
        },
    )

    assert response.status == 200
    assert response.body["settings"]["route_offset_xyz_m"] == [2.0, -1.0, 5.0]
    snapshot = api.handle("GET", "/api/projects/p1/snapshot").body
    assert snapshot["clips"][0]["srt_fixed_track_visual_pose_settings"] == (
        response.body["settings"]
    )


def test_adapter_parameters_bind_georeference_media_fov_and_offset(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = _confirmed_service(tmp_path)
    clips = repositories.clips.load("p1")
    service.update_srt_fixed_track_visual_pose_settings(
        "p1",
        "clip-1",
        expected_revision=clips.revision,
        horizontal_fov_deg=72.0,
        route_offset_xyz_m=(2.0, -1.0, 5.0),
    )
    project = repositories.project.load("p1")
    selected = repositories.clips.load("p1").clips[0]

    parameters = _fixed_track_visual_pose_adapter_parameters(
        service.projects_root, project, selected
    )

    assert parameters["cad_georeference"]["confirmed"] is True
    assert parameters["cad_origin_xy"] == [499000.0, 3319000.0]
    assert parameters["cad_scale"] == 1.0
    assert parameters["video_metadata"]["fps"] == 25.0
    assert parameters["srt_fixed_track_visual_pose"] == {
        "schema_version": 1,
        "horizontal_fov_deg": 72.0,
        "route_offset_xyz_m": [2.0, -1.0, 5.0],
    }
