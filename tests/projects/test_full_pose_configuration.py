from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path

from cadscene.projects.http_api import ProjectApi
from cadscene.projects.media import ProjectMediaSpec
from cadscene.projects.service import (
    _job_identity_payload,
    _srt_full_pose_adapter_parameters,
)
from cadscene.projects.uploads import ValidatedUploadStore

from .test_service_jobs import clip, service_with_clips


def _srt_text() -> str:
    return """1
00:00:00,000 --> 00:00:00,040
[latitude: 30.000000] [longitude: 120.000000] [rel_alt: 60.0] [gimbal_yaw: 0.0] [gimbal_pitch: -90.0] [gimbal_roll: 0.0]

2
00:00:00,040 --> 00:00:00,080
[latitude: 30.000010] [longitude: 120.000010] [rel_alt: 60.2] [gimbal_yaw: 0.2] [gimbal_pitch: -89.8] [gimbal_roll: 0.0]

3
00:00:00,080 --> 00:00:00,120
[latitude: 30.000020] [longitude: 120.000020] [rel_alt: 60.4] [gimbal_yaw: 0.4] [gimbal_pitch: -89.6] [gimbal_roll: 0.0]
"""


def _configure_project_inputs(tmp_path: Path, repositories) -> Path:
    srt_path = tmp_path / "flight.srt"
    srt_path.write_text(_srt_text(), encoding="utf-8")
    cad_dataset = tmp_path / "cad-dataset"
    cad_dataset.mkdir()
    (cad_dataset / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "cad": {
                    "bbox": [499000.0, 3319000.0, 501000.0, 3322000.0]
                },
                "defaults": {
                    "cad_scale": 1.0,
                    "origin_xy": [499000.0, 3319000.0],
                },
            }
        ),
        encoding="utf-8",
    )
    project = repositories.project.load("p1")
    media_spec = ProjectMediaSpec(
        width=3840,
        height=2160,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 25),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=Fraction(25, 1),
    )
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={
                **current.source_assets,
                "srt_path": str(srt_path),
                "cad": {
                    "path": str(tmp_path / "site.dxf"),
                    "sha256": "c" * 64,
                    "dataset_path": str(cad_dataset),
                },
            },
            media_spec_revision="media-spec-test",
            media_spec=media_spec.to_dict(),
        ),
    )
    return srt_path


def test_recommends_cgcs2000_projection_without_auto_confirming(tmp_path: Path) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)

    candidates = service.recommend_cad_georeference("p1")

    assert candidates
    assert candidates[0].epsg == 4549
    assert candidates[0].central_meridian_deg == 120.0
    assert candidates[0].cad_axis_mapping == "cad_x_easting_cad_y_northing"
    assert candidates[0].confirmed is False


def test_confirmation_is_bound_to_current_cad_and_replacement_invalidates_it(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    candidate = service.recommend_cad_georeference("p1")[0]
    project = repositories.project.load("p1")

    confirmed = service.confirm_cad_georeference(
        "p1",
        candidate.to_dict(),
        expected_revision=project.revision,
    )

    stored = confirmed.source_assets["_cad_georeference"]
    assert stored["confirmed"] is True
    assert stored["epsg"] == 4549
    assert stored["cad_asset_fingerprint"]

    current = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=current.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "cad": {**value.source_assets["cad"], "sha256": "d" * 64},
            },
        ),
    )

    preflight = service.preflight_trajectory_jobs("p1")
    assert preflight.skipped == ("clip-1",)
    assert "current CAD" in preflight.reasons["clip-1"]


def test_updates_single_horizontal_fov_and_exposes_versioned_api_snapshot(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    project = repositories.project.load("p1")
    candidate = service.recommend_cad_georeference("p1")[0]
    service.confirm_cad_georeference(
        "p1", candidate.to_dict(), expected_revision=project.revision
    )
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(tmp_path / "projects"),
        now=lambda: "2026-09-01T00:00:00Z",
    )

    response = api.handle(
        "PATCH",
        "/api/projects/p1/clips/clip-1/srt-full-pose",
        json_body={
            "expected_revision": repositories.clips.load("p1").revision,
            "horizontal_fov_deg": 120.0,
            "cad_z_offset_m": 1.5,
        },
    )

    assert response.status == 200
    assert response.body["settings"] == {
        "horizontal_fov_deg": 120.0,
        "cad_z_offset_m": 1.5,
        "attitude_profile": "dji_absolute_ned",
    }
    snapshot = api.handle("GET", "/api/projects/p1/snapshot").body
    assert snapshot["cad_georeference"]["confirmed"] is True
    assert snapshot["clips"][0]["srt_full_pose_settings"] == response.body["settings"]


def test_candidate_and_confirmation_routes_use_project_revision(tmp_path: Path) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    api = ProjectApi(
        repositories=repositories,
        service=service,
        uploads=ValidatedUploadStore(tmp_path / "projects"),
        now=lambda: "2026-09-01T00:00:00Z",
    )

    candidates_response = api.handle(
        "POST", "/api/projects/p1/cad-georeference/candidates"
    )
    candidate = candidates_response.body["candidates"][0]
    confirm_response = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate": candidate,
        },
    )

    assert candidates_response.status == 200
    assert candidate["confirmed"] is False
    assert confirm_response.status == 200
    assert confirm_response.body["cad_georeference"]["confirmed"] is True


def test_job_parameters_merge_confirmed_georeference_cad_defaults_and_video(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    project = repositories.project.load("p1")
    service.confirm_cad_georeference(
        "p1",
        service.recommend_cad_georeference("p1")[0].to_dict(),
        expected_revision=project.revision,
    )
    clips = repositories.clips.load("p1")
    service.update_srt_full_pose_settings(
        "p1",
        "clip-1",
        expected_revision=clips.revision,
        horizontal_fov_deg=120.0,
    )
    project = repositories.project.load("p1")
    selected = repositories.clips.load("p1").clips[0]

    parameters = _srt_full_pose_adapter_parameters(
        service.projects_root, project, selected
    )
    identity = _job_identity_payload(
        job_type="trajectory",
        clip=selected,
        project_assets=project.source_assets,
        project_revision=project.revision,
        clips_revision=repositories.clips.load("p1").revision,
        adapter_name="srt_full_pose",
        adapter_version="2",
    )

    assert parameters["cad_origin_xy"] == [499000.0, 3319000.0]
    assert parameters["cad_scale"] == 1.0
    assert parameters["video_metadata"] == {
        "width": 3840,
        "height": 2160,
        "fps": 25.0,
    }
    assert parameters["srt_full_pose"]["horizontal_fov_deg"] == 120.0
    assert identity["cad_georeference"]["revision"].startswith("cad-georef-")
    assert service.preflight_trajectory_jobs("p1").eligible == ("clip-1",)
