from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from cadscene.projects.repositories import RevisionConflict
from cadscene.projects.workbench_resume import AtomicWorkbenchResumeStore


NOW = datetime(2026, 8, 31, 1, 2, 3, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _create(store: AtomicWorkbenchResumeStore):
    return store.update(
        "project-1",
        "clip-1",
        expected_revision=None,
        operation_id="resume-op-1",
        workflow_stage="keyframes",
        source_pts=104,
        source_time_base={"numerator": 1, "denominator": 1000},
        trajectory_output_revision="trajectory-1",
        workbench_output_revision=None,
        quality_revision=None,
        render_revision=None,
    )


def test_resume_store_returns_none_when_record_is_missing(tmp_path: Path) -> None:
    store = AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock)

    assert store.load_optional("project-1", "clip-1") is None


def test_resume_store_survives_reconstruction_and_checks_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    first = AtomicWorkbenchResumeStore(root, now=_clock)
    created = _create(first)

    assert created.revision == 0
    assert created.source_pts == 104
    assert created.source_time_base == {"numerator": 1, "denominator": 1000}
    assert AtomicWorkbenchResumeStore(root, now=_clock).load_optional(
        "project-1", "clip-1"
    ) == created

    with pytest.raises(RevisionConflict):
        first.update(
            "project-1",
            "clip-1",
            expected_revision=9,
            operation_id="resume-op-2",
            workflow_stage="quality",
            source_pts=111,
            source_time_base={"numerator": 1, "denominator": 1000},
            trajectory_output_revision="trajectory-1",
            workbench_output_revision=None,
            quality_revision="quality-1",
            render_revision=None,
        )

    updated = first.update(
        "project-1",
        "clip-1",
        expected_revision=created.revision,
        operation_id="resume-op-3",
        workflow_stage="quality",
        source_pts=111,
        source_time_base={"numerator": 1, "denominator": 1000},
        trajectory_output_revision="trajectory-1",
        workbench_output_revision=None,
        quality_revision="quality-1",
        render_revision=None,
    )
    assert updated.revision == 1
    assert updated.workflow_stage == "quality"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workflow_stage", "upload"),
        ("source_pts", True),
        ("source_time_base", {"numerator": 0, "denominator": 1000}),
        ("source_time_base", {"numerator": 1, "denominator": 0}),
    ),
)
def test_resume_store_rejects_invalid_authority_fields(
    tmp_path: Path, field: str, value: object
) -> None:
    store = AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock)
    arguments = {
        "expected_revision": None,
        "operation_id": "resume-op-1",
        "workflow_stage": "keyframes",
        "source_pts": 104,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "trajectory_output_revision": "trajectory-1",
        "workbench_output_revision": None,
        "quality_revision": None,
        "render_revision": None,
    }
    arguments[field] = value

    with pytest.raises((TypeError, ValueError)):
        store.update("project-1", "clip-1", **arguments)


@pytest.mark.parametrize("payload", ("{", json.dumps({"schema_version": "9"})))
def test_resume_store_ignores_corrupt_or_unsupported_records(
    tmp_path: Path, payload: str
) -> None:
    store = AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock)
    path = store.path_for("project-1", "clip-1")
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    assert store.load_optional("project-1", "clip-1") is None


def test_resume_store_uses_project_local_clip_path(tmp_path: Path) -> None:
    store = AtomicWorkbenchResumeStore(tmp_path / "projects", now=_clock)

    assert store.path_for("project-1", "clip-1") == (
        tmp_path / "projects/project-1/workbench_resume/clip-1.json"
    )
    with pytest.raises(ValueError):
        store.path_for("project-1", "../clip-1")
