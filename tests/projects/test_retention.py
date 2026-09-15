from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.projects.retention import (
    prune_sfm_workspace,
    reclaim_terminal_attempt,
)


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def test_prune_sfm_workspace_removes_only_recreatable_colmap_children(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "02_sfm"
    _write(stage / "images/frame-0001.jpg", b"image")
    _write(stage / "masks/frame-0001.png", b"mask")
    _write(stage / "database.db", b"database")
    _write(stage / "sparse/0/cameras.bin", b"sparse")
    _write(stage / "sparse_text_export/cameras.txt", b"text-export")
    formal = {
        "camera_trajectory.json": b"trajectory",
        "sparse_points.ply": b"points",
        "camera_intrinsics.json": b"intrinsics",
        "sfm_stats.json": b"stats",
        "sfm_report.md": b"report",
    }
    for name, payload in formal.items():
        _write(stage / name, payload)

    report = prune_sfm_workspace(stage)

    assert set(report.removed_paths) == {
        "database.db",
        "images",
        "masks",
        "sparse",
        "sparse_text_export",
    }
    assert report.reclaimed_bytes == sum(
        len(payload)
        for payload in (b"image", b"mask", b"database", b"sparse", b"text-export")
    )
    assert report.errors == ()
    for name, payload in formal.items():
        assert (stage / name).read_bytes() == payload

    repeated = prune_sfm_workspace(stage)
    assert repeated.reclaimed_bytes == 0
    assert repeated.removed_paths == ()
    assert repeated.errors == ()


def test_prune_sfm_workspace_never_follows_an_outside_link(tmp_path: Path) -> None:
    stage = tmp_path / "02_sfm"
    stage.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "must-remain.bin", b"safe")
    try:
        (stage / "images").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks is unavailable on this Windows host")

    report = prune_sfm_workspace(stage)

    assert (outside / "must-remain.bin").read_bytes() == b"safe"
    assert report.reclaimed_bytes == 0
    assert report.removed_paths == ()
    assert report.errors


def test_failed_attempt_reclaims_payload_but_keeps_small_diagnostics(
    tmp_path: Path,
) -> None:
    attempt = tmp_path / "projects/p1/jobs/j1/attempt-1"
    _write(attempt / "adapter.log", b"failure details")
    _write(attempt / "commands.json", b"{}")
    _write(attempt / "nested/sfm_report.md", b"diagnosis")
    _write(attempt / "nested/frame-0001.jpg", b"frame")
    _write(attempt / "nested/database.db", b"database")
    _write(attempt / "partial.mp4", b"video")

    report = reclaim_terminal_attempt(
        attempt,
        status="failed",
        job_type="trajectory",
        published_outputs={},
    )

    assert (attempt / "adapter.log").read_bytes() == b"failure details"
    assert (attempt / "commands.json").read_bytes() == b"{}"
    assert (attempt / "nested/sfm_report.md").read_bytes() == b"diagnosis"
    assert not (attempt / "nested/frame-0001.jpg").exists()
    assert not (attempt / "nested/database.db").exists()
    assert not (attempt / "partial.mp4").exists()
    assert report.reclaimed_bytes == len(b"frame") + len(b"database") + len(b"video")
    persisted = json.loads((attempt / "retention_report.json").read_text("utf-8"))
    assert persisted["status"] == "failed"
    assert persisted["job_type"] == "trajectory"
    assert persisted["reclaimed_bytes"] == report.reclaimed_bytes


@pytest.mark.parametrize(
    "job_type", ("trajectory", "clip_export", "sfm_solve_export", "project_merge")
)
def test_successful_authoritative_attempts_are_not_reclaimed(
    tmp_path: Path,
    job_type: str,
) -> None:
    attempt = tmp_path / job_type / "attempt-1"
    payload = _write(attempt / "authoritative.bin", b"authoritative")

    report = reclaim_terminal_attempt(
        attempt,
        status="success",
        job_type=job_type,
        published_outputs={"output": str(payload)},
    )

    assert payload.read_bytes() == b"authoritative"
    assert report.reclaimed_bytes == 0
    assert report.removed_paths == ()
    assert not (attempt / "retention_report.json").exists()


@pytest.mark.parametrize("job_type", ("clip_render", "scene_bridge"))
def test_successful_immutable_publication_reclaims_attempt_payload(
    tmp_path: Path,
    job_type: str,
) -> None:
    attempt = tmp_path / "projects/p1/jobs/j1/attempt-1"
    _write(attempt / "adapter.log", b"completed")
    duplicate = _write(attempt / "output/rendered.mp4", b"duplicate-video")
    published = _write(
        tmp_path / f"projects/p1/published/{job_type}/rendered.mp4",
        b"published-video",
    )

    report = reclaim_terminal_attempt(
        attempt,
        status="success",
        job_type=job_type,
        published_outputs={"video": str(published)},
    )

    assert not duplicate.exists()
    assert published.read_bytes() == b"published-video"
    assert (attempt / "adapter.log").read_bytes() == b"completed"
    assert report.reclaimed_bytes == len(b"duplicate-video")


def test_successful_publication_inside_attempt_is_not_reclaimed(tmp_path: Path) -> None:
    attempt = tmp_path / "projects/p1/jobs/j1/attempt-1"
    output = _write(attempt / "rendered.mp4", b"only-copy")

    report = reclaim_terminal_attempt(
        attempt,
        status="success",
        job_type="clip_render",
        published_outputs={"video": str(output)},
    )

    assert output.read_bytes() == b"only-copy"
    assert report.reclaimed_bytes == 0
    assert report.removed_paths == ()
    assert report.errors


def test_non_terminal_attempt_is_unchanged(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt-1"
    payload = _write(attempt / "partial.mp4", b"running")

    report = reclaim_terminal_attempt(
        attempt,
        status="running",
        job_type="clip_render",
        published_outputs={},
    )

    assert payload.read_bytes() == b"running"
    assert report.reclaimed_bytes == 0
    assert report.removed_paths == ()
    assert not (attempt / "retention_report.json").exists()


def test_terminal_attempt_reclaim_is_idempotent(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt-1"
    _write(attempt / "partial.mp4", b"video")

    first = reclaim_terminal_attempt(
        attempt,
        status="cancelled",
        job_type="clip_render",
        published_outputs={},
    )
    second = reclaim_terminal_attempt(
        attempt,
        status="cancelled",
        job_type="clip_render",
        published_outputs={},
    )

    assert first.reclaimed_bytes == len(b"video")
    assert second.reclaimed_bytes == 0
    assert second.errors == ()
