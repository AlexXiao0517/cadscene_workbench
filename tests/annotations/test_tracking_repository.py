from __future__ import annotations

from pathlib import Path

import pytest

from cadscene.annotations.tracking import (
    OpenCvLkVideoAnchorTracker,
    TrackingFrame,
    TrackingResult,
    TrackingRevision,
    TrackingRevisionRepository,
    VideoTrackingService,
    VideoTrackingInitialization,
)
from cadscene.annotations.service import AnnotationService
from cadscene.annotations.models import SourcePtsRange
from cadscene.projects.json_repositories import project_repositories
from cadscene.projects.models import ClipDefinition
from dataclasses import replace
from threading import RLock
import cv2
import numpy as np


def _revision(revision_id: str = "tracking-1") -> TrackingRevision:
    return TrackingRevision(
        tracking_revision=revision_id,
        annotation_id="label-1",
        clip_id="clip-1",
        tracker_name="opencv_sparse_lk",
        tracker_version="1",
        source_video_fingerprint="sha256:source",
        clip_revision=3,
        source_start_pts=100,
        source_end_pts_exclusive=200,
        time_base_numerator=1,
        time_base_denominator=25,
        initialization=VideoTrackingInitialization(
            source_pts=100, bbox=(20.0, 30.0, 40.0, 20.0)
        ),
        corrections=(),
        results=(
            TrackingResult(
                source_pts=100,
                bbox=(20.0, 30.0, 40.0, 20.0),
                anchor_xy=(40.0, 40.0),
                confidence=1.0,
                visible=True,
                tracking_status="initialized",
                diagnostic="initialized",
            ),
        ),
        created_at="now",
        operation_id="operation-1",
    )


def test_tracking_revision_repository_is_immutable_and_keeps_old_revisions(
    tmp_path: Path,
) -> None:
    repository = TrackingRevisionRepository(tmp_path / "projects")

    first_path = repository.publish("p1", _revision("tracking-1"))
    second_path = repository.publish("p1", _revision("tracking-2"))

    assert repository.load("p1", "clip-1", "label-1", "tracking-1") == _revision(
        "tracking-1"
    )
    assert repository.load("p1", "clip-1", "label-1", "tracking-2") == _revision(
        "tracking-2"
    )
    assert first_path.name == second_path.name == "tracking_results.json"
    with pytest.raises(FileExistsError, match="immutable tracking revision"):
        repository.publish("p1", _revision("tracking-1"))


def test_tracking_revision_rejects_out_of_order_correction_keyframes() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        replace(
            _revision(),
            corrections=(
                VideoTrackingInitialization(
                    source_pts=160, bbox=(40.0, 30.0, 40.0, 20.0)
                ),
                VideoTrackingInitialization(
                    source_pts=140, bbox=(50.0, 30.0, 40.0, 20.0)
                ),
            ),
        )


def _frame(source_pts: int, x: int | None) -> TrackingFrame:
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    if x is not None:
        for row in range(3):
            for column in range(4):
                cv2.circle(
                    image,
                    (x + 4 + column * 7, 24 + row * 7),
                    2,
                    (255, 255, 255),
                    -1,
                )
    return TrackingFrame(source_pts, image)


def test_reanchor_creates_new_revision_and_preserves_lost_gap_and_old_artifact(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    repositories = project_repositories(projects)
    repositories.create_project("p1", updated_at="now")
    clip = ClipDefinition.from_analysis(
        {
            "project_id": "p1",
            "clip_id": "clip-1",
            "analysis_revision": "analysis-1",
            "source_start_pts": 100,
            "source_end_pts_exclusive": 130,
            "source_time_base": {"numerator": 1, "denominator": 25},
            "recommended_workflow": "sfm_only",
        },
        generated_display_name="Scene 1",
    )
    clips = repositories.clips.load("p1")
    repositories.clips.update(
        "p1",
        expected_revision=clips.revision,
        mutate=lambda value: replace(
            value, analysis_revision="analysis-1", clips=(clip,)
        ),
    )
    identities = iter(("annotation", "revision-1", "operation-1", "revision-2", "operation-2"))
    annotation_service = AnnotationService(
        repositories,
        now=lambda: "now",
        identity=lambda: next(identities),
        publication_lock=RLock(),
    )
    created = annotation_service.create(
        "p1",
        expected_revision=0,
        annotation_id="label-1",
        clip_id="clip-1",
        anchor_type="video_track",
        text="vehicle",
        anchor={"initialization": {"source_pts": 100, "bbox": [20, 20, 28, 20]}},
        source_pts_range=SourcePtsRange(100, 130, 1, 25),
    )
    tracking = VideoTrackingService(
        repositories=repositories,
        annotations=annotation_service,
        revisions=TrackingRevisionRepository(projects),
        tracker=OpenCvLkVideoAnchorTracker(min_features=4),
        now=lambda: "now",
        identity=lambda: next(identities),
    )
    frames = (
        _frame(100, 20),
        _frame(105, 24),
        _frame(110, None),
        _frame(115, None),
        _frame(120, 50),
        _frame(125, 54),
    )

    first = tracking.create_revision(
        "p1",
        "label-1",
        expected_revision=created.manifest_revision,
        expected_annotation_revision=0,
        source_video_fingerprint="sha256:source",
        frames=frames,
    )
    second = tracking.create_revision(
        "p1",
        "label-1",
        expected_revision=first.annotation_result.manifest_revision,
        expected_annotation_revision=1,
        source_video_fingerprint="sha256:source",
        frames=frames,
        correction=VideoTrackingInitialization(
            source_pts=120, bbox=(50.0, 20.0, 28.0, 20.0)
        ),
    )

    assert first.revision.results[2].visible is False
    assert second.revision.results[2].visible is False
    assert second.revision.results[3].visible is False
    assert second.revision.results[4].tracking_status == "initialized"
    assert second.revision.results[5].visible is True
    assert first.revision.tracking_revision != second.revision.tracking_revision
    assert first.artifact_path.is_file()
    assert second.artifact_path.is_file()
    active = repositories.annotations.load("p1").annotations[0]
    assert active.active_tracking_revision == second.revision.tracking_revision
    assert active.annotation_revision == 2
