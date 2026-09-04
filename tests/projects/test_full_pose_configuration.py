from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path

import pytest

from cadscene.projects.http_api import ProjectApi
from cadscene.projects.executor import LocalJobExecutor
from cadscene.projects.media import ProjectMediaSpec
from cadscene.projects.service import (
    _job_identity_payload,
    _srt_full_pose_adapter_parameters,
)
from cadscene.projects.uploads import ValidatedUploadStore

from .test_service_jobs import clip, service_with_clips


def _javascript_json_number_roundtrip(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_javascript_json_number_roundtrip(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _javascript_json_number_roundtrip(item)
            for key, item in value.items()
        }
    return value


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
    assert "trajectory_polyline_raw" not in stored["validation"]

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


def test_full_pose_fov_is_rounded_and_stored_as_an_integer(tmp_path: Path) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )

    updated = service.update_srt_full_pose_settings(
        "p1",
        "clip-1",
        expected_revision=repositories.clips.load("p1").revision,
        horizontal_fov_deg=59.11,
    )

    stored = updated.clips[0].manual_definition["srt_full_pose"][
        "horizontal_fov_deg"
    ]
    assert stored == 59
    assert isinstance(stored, int)


@pytest.mark.parametrize("horizontal_fov_deg", [1.49, 178.51])
def test_full_pose_fov_rejects_values_that_round_outside_integer_range(
    tmp_path: Path,
    horizontal_fov_deg: float,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )

    with pytest.raises(ValueError, match=r"\[2, 178\]"):
        service.update_srt_full_pose_settings(
            "p1",
            "clip-1",
            expected_revision=repositories.clips.load("p1").revision,
            horizontal_fov_deg=horizontal_fov_deg,
        )


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

    project_revision = repositories.project.load("p1").revision
    candidates_response = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": project_revision,
            "central_meridian_deg": 120.0,
        },
    )
    repeated = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": project_revision,
            "central_meridian_deg": 120.0,
        },
    )
    queued = api.handle("GET", "/api/projects/p1/cad-georeference/candidates")

    assert candidates_response.status == 202
    assert repeated.body["operation"]["job_id"] == candidates_response.body[
        "operation"
    ]["job_id"]
    assert queued.body["operation"]["status"] in {"queued", "running"}
    assert queued.body["operation"]["requested_central_meridian_deg"] == 120.0

    finished = LocalJobExecutor(service).run_next()
    assert finished is not None and finished.status == "success"
    completed = api.handle("GET", "/api/projects/p1/cad-georeference/candidates")
    operation = completed.body["operation"]
    candidate = operation["candidates"][0]
    confirm_response = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate_job_id": operation["job_id"],
            "candidate_input_fingerprint": operation["input_fingerprint"],
            "candidate": candidate,
        },
    )

    assert completed.status == 200
    assert operation["status"] == "success"
    assert operation["progress"] == {
        "stage": "complete",
        "message": "坐标系候选生成完成",
        "fraction": 1.0,
    }
    assert candidate["confirmed"] is False
    assert confirm_response.status == 200
    assert confirm_response.body["cad_georeference"]["confirmed"] is True


def test_candidate_confirmation_accepts_javascript_json_number_roundtrip(
    tmp_path: Path,
) -> None:
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
    started = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "central_meridian_deg": 120.0,
        },
    )
    assert started.status == 202
    finished = LocalJobExecutor(service).run_next()
    assert finished is not None and finished.status == "success"
    operation = api.handle(
        "GET", "/api/projects/p1/cad-georeference/candidates"
    ).body["operation"]
    browser_candidate = _javascript_json_number_roundtrip(
        operation["candidates"][0]
    )
    assert isinstance(browser_candidate["score"], int)

    tampered = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate_job_id": operation["job_id"],
            "candidate_input_fingerprint": operation["input_fingerprint"],
            "candidate": {
                **browser_candidate,
                "central_meridian_deg": 119.0,
            },
        },
    )
    assert tampered.status == 400
    assert "current result" in tampered.body["error"]

    confirmed = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate_job_id": operation["job_id"],
            "candidate_input_fingerprint": operation["input_fingerprint"],
            "candidate": browser_candidate,
        },
    )

    assert confirmed.status == 200
    assert confirmed.body["cad_georeference"]["confirmed"] is True


def test_degree_minute_candidate_request_persists_custom_projection(
    tmp_path: Path,
) -> None:
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
    project_revision = repositories.project.load("p1").revision

    started = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": project_revision,
            "central_meridian_deg": "118°50′",
        },
    )
    repeated = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": project_revision,
            "central_meridian_deg": 118.0 + 50.0 / 60.0,
        },
    )

    assert started.status == 202
    assert repeated.body["operation"]["job_id"] == started.body["operation"][
        "job_id"
    ]
    assert started.body["operation"][
        "requested_central_meridian_deg"
    ] == pytest.approx(118.0 + 50.0 / 60.0)

    finished = LocalJobExecutor(service).run_next()
    assert finished is not None and finished.status == "success"
    operation = api.handle(
        "GET", "/api/projects/p1/cad-georeference/candidates"
    ).body["operation"]
    candidate = operation["candidates"][0]
    assert candidate["crs_source"] == "custom"
    assert candidate["epsg"] is None
    assert candidate["central_meridian_deg"] == pytest.approx(
        118.0 + 50.0 / 60.0
    )

    confirmed = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate_job_id": operation["job_id"],
            "candidate_input_fingerprint": operation["input_fingerprint"],
            "candidate": candidate,
        },
    )

    assert confirmed.status == 200
    config = confirmed.body["cad_georeference"]
    assert config["crs_source"] == "custom"
    assert config["epsg"] is None
    assert config["false_easting_m"] == 500_000.0
    assert config["ellipsoid"] == "GRS80"


def test_candidate_request_recovers_cad_dataset_from_active_clip_snapshot(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    dataset_path = tmp_path / "cad-dataset"
    project = repositories.project.load("p1")
    cad = dict(project.source_assets["cad"])
    cad.pop("dataset_path")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={**current.source_assets, "cad": cad},
        ),
    )
    clips = repositories.clips.load("p1")
    active_clip = clips.clips[0]
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda current: replace(
            current,
            clips=(
                replace(
                    active_clip,
                    analysis={
                        **active_clip.analysis,
                        "input_snapshot": {
                            "cad": {
                                "sha256": "c" * 64,
                                "dataset_id": "cad-analysis-1",
                                "dataset_path": str(dataset_path),
                            }
                        },
                    },
                ),
            ),
        ),
    )

    current = repositories.project.load("p1")
    job = service.enqueue_cad_georeference_candidates(
        "p1",
        expected_revision=current.revision,
        central_meridian_deg=120.0,
    )
    request = json.loads(
        (
            Path(job.attempts[-1].directory)
            / "cad_georeference_candidate_request.json"
        ).read_text(encoding="utf-8")
    )

    assert request["cad_bbox_raw"] == [
        499000.0,
        3319000.0,
        501000.0,
        3322000.0,
    ]
    assert service.cad_georeference_candidate_status("p1")["stale"] is False
    assert service.recommend_cad_georeference(
        "p1", central_meridian_deg=120.0
    )


def test_candidate_request_prefers_viewer_focus_bbox_from_cad_import_stats(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    focus_bbox = [484717.0, 3189945.0, 549016.0, 3211687.0]
    (tmp_path / "cad-dataset" / "cad_import_stats.json").write_text(
        json.dumps(
            {
                "bbox": [-3249624.0, -1907080.0, 3245569.0, 3276235.0],
                "viewer_focus_bbox": focus_bbox,
            }
        ),
        encoding="utf-8",
    )

    current = repositories.project.load("p1")
    job = service.enqueue_cad_georeference_candidates(
        "p1",
        expected_revision=current.revision,
        central_meridian_deg=120.0,
    )
    request = json.loads(
        (
            Path(job.attempts[-1].directory)
            / "cad_georeference_candidate_request.json"
        ).read_text(encoding="utf-8")
    )

    assert request["cad_bbox_raw"] == focus_bbox


def test_candidate_request_rejects_snapshot_from_a_different_cad(
    tmp_path: Path,
) -> None:
    service, repositories, _queue = service_with_clips(
        tmp_path, (clip("clip-1", workflow="srt_full_pose"),)
    )
    _configure_project_inputs(tmp_path, repositories)
    project = repositories.project.load("p1")
    cad = dict(project.source_assets["cad"])
    cad.pop("dataset_path")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda current: replace(
            current,
            source_assets={**current.source_assets, "cad": cad},
        ),
    )
    clips = repositories.clips.load("p1")
    active_clip = clips.clips[0]
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda current: replace(
            current,
            clips=(
                replace(
                    active_clip,
                    analysis={
                        **active_clip.analysis,
                        "input_snapshot": {
                            "cad": {
                                "sha256": "d" * 64,
                                "dataset_id": "cad-analysis-old",
                                "dataset_path": str(tmp_path / "cad-dataset"),
                            }
                        },
                    },
                ),
            ),
        ),
    )

    current = repositories.project.load("p1")
    with pytest.raises(ValueError, match="coordinate bbox"):
        service.enqueue_cad_georeference_candidates(
            "p1",
            expected_revision=current.revision,
            central_meridian_deg=120.0,
        )


def test_candidate_status_and_confirmation_reject_changed_cad_input(
    tmp_path: Path,
) -> None:
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
    started = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/candidates",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "central_meridian_deg": 120.0,
        },
    )
    finished = LocalJobExecutor(service).run_next()
    assert finished is not None and finished.status == "success"
    operation = api.handle(
        "GET", "/api/projects/p1/cad-georeference/candidates"
    ).body["operation"]
    candidate = operation["candidates"][0]
    project = repositories.project.load("p1")
    repositories.project.update(
        "p1",
        expected_revision=project.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "cad": {**value.source_assets["cad"], "sha256": "d" * 64},
            },
        ),
    )

    stale = api.handle("GET", "/api/projects/p1/cad-georeference/candidates")
    rejected = api.handle(
        "POST",
        "/api/projects/p1/cad-georeference/confirm",
        json_body={
            "expected_revision": repositories.project.load("p1").revision,
            "candidate_job_id": started.body["operation"]["job_id"],
            "candidate_input_fingerprint": operation["input_fingerprint"],
            "candidate": candidate,
        },
    )

    assert stale.body["operation"]["status"] == "stale_input"
    assert stale.body["operation"]["stale"] is True
    assert stale.body["operation"]["candidates"] == []
    assert rejected.status == 400
    assert "stale" in rejected.body["error"]


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


def test_missing_full_pose_frame_map_is_reported_and_regenerated_before_adapter(
    tmp_path: Path,
) -> None:
    physical_clip = tmp_path / "clip-1.mp4"
    physical_clip.write_bytes(b"mp4")
    selected = clip("clip-1", workflow="srt_full_pose")
    selected = replace(
        selected,
        analysis={
            **selected.analysis,
            "physical_mp4_path": str(physical_clip),
            "frame_map_path": str(tmp_path / "missing-frame-map.json"),
        },
        manual_definition={
            **selected.manual_definition,
            "srt_full_pose": {
                "horizontal_fov_deg": 82.0,
                "cad_z_offset_m": 0.0,
                "attitude_profile": "dji_absolute_ned",
            },
        },
    )
    service, repositories, queue = service_with_clips(tmp_path, (selected,))
    _configure_project_inputs(tmp_path, repositories)
    project = repositories.project.load("p1")
    service.confirm_cad_georeference(
        "p1",
        service.recommend_cad_georeference("p1")[0].to_dict(),
        expected_revision=project.revision,
    )

    preflight = service.preflight_trajectory_jobs("p1")
    result = service.enqueue_trajectory_jobs("p1")
    jobs = queue.jobs()

    assert preflight.eligible == ("clip-1",)
    assert "frame map" in preflight.reasons["clip-1"]
    assert [job.job_type for job in jobs] == ["clip_export", "trajectory"]
    assert jobs[1].depends_on_job_ids == (jobs[0].job_id,)
    assert result.enqueued_clip_ids == ("clip-1",)
