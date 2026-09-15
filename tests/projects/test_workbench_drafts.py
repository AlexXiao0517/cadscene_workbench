from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cadscene.projects.repositories import RevisionConflict
from cadscene.projects.workbench_drafts import AtomicWorkbenchDraftStore


NOW = datetime(2026, 9, 3, 2, 3, 4, tzinfo=timezone.utc)


def _track(x: float = 1.0) -> dict[str, object]:
    return {
        "version": 1,
        "fps": 25.0,
        "keyframes": [
            {
                "frame": 0,
                "time": 0.0,
                "source": "manual_anchor",
                "camera": {
                    "x": x,
                    "y": 2.0,
                    "z": 3.0,
                    "yaw": 4.0,
                    "pitch": -20.0,
                    "roll": 0.0,
                    "fov": 60.0,
                },
            }
        ],
    }


def _create(store: AtomicWorkbenchDraftStore):
    return store.update(
        "project-1",
        "clip-1",
        expected_revision=None,
        operation_id="draft-op-1",
        workflow="sfm_only",
        project_input_revision="analysis-1",
        clip_input_revision="analysis-1",
        trajectory_output_revision="trajectory-1",
        trajectory_output_fingerprint="f" * 64,
        camera_track=_track(),
    )


def test_draft_store_survives_reconstruction_and_checks_revision(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    created = _create(AtomicWorkbenchDraftStore(root, now=lambda: NOW))

    assert created.revision == 0
    assert created.promoted_to_workbench_output_revision is None
    assert AtomicWorkbenchDraftStore(root).load_optional("project-1", "clip-1") == created

    with pytest.raises(RevisionConflict):
        AtomicWorkbenchDraftStore(root).update(
            "project-1",
            "clip-1",
            expected_revision=8,
            operation_id="draft-op-2",
            workflow="sfm_only",
            project_input_revision="analysis-1",
            clip_input_revision="analysis-1",
            trajectory_output_revision="trajectory-1",
            trajectory_output_fingerprint="f" * 64,
            camera_track=_track(9.0),
        )


def test_draft_store_only_returns_unpromoted_draft_for_exact_input_binding(
    tmp_path: Path,
) -> None:
    store = AtomicWorkbenchDraftStore(tmp_path / "projects", now=lambda: NOW)
    created = _create(store)

    assert store.load_compatible(
        "project-1",
        "clip-1",
        workflow="sfm_only",
        project_input_revision="analysis-1",
        clip_input_revision="analysis-1",
        trajectory_output_revision="trajectory-1",
        trajectory_output_fingerprint="f" * 64,
    ) == created
    assert store.load_compatible(
        "project-1",
        "clip-1",
        workflow="sfm_only",
        project_input_revision="analysis-1",
        clip_input_revision="analysis-2",
        trajectory_output_revision="trajectory-1",
        trajectory_output_fingerprint="f" * 64,
    ) is None

    promoted = store.mark_promoted(
        "project-1",
        "clip-1",
        expected_revision=created.revision,
        operation_id="promote-op-1",
        workbench_output_revision="workbench-1",
    )
    assert promoted.revision == 1
    assert promoted.promoted_to_workbench_output_revision == "workbench-1"
    assert store.load_compatible(
        "project-1",
        "clip-1",
        workflow="sfm_only",
        project_input_revision="analysis-1",
        clip_input_revision="analysis-1",
        trajectory_output_revision="trajectory-1",
        trajectory_output_fingerprint="f" * 64,
    ) is None


def test_draft_store_uses_small_project_local_record(tmp_path: Path) -> None:
    store = AtomicWorkbenchDraftStore(tmp_path / "projects", now=lambda: NOW)
    _create(store)

    path = tmp_path / "projects/project-1/workbench_drafts/clip-1.json"
    assert store.path_for("project-1", "clip-1") == path
    assert path.is_file()
    assert path.stat().st_size < 16_384
    assert not list(path.parent.glob("*.tmp"))
