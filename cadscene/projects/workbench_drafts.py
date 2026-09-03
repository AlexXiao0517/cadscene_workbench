from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Lock, RLock
from typing import Callable, Mapping

from .identifiers import is_safe_stable_id, validate_project_id
from .repositories import RevisionConflict


DRAFT_SCHEMA_VERSION = "1.0"
_LOCKS_GUARD = Lock()
_RECORD_LOCKS: dict[str, RLock] = {}


@dataclass(frozen=True)
class WorkbenchDraft:
    schema_version: str
    revision: int
    operation_id: str
    updated_at: str
    project_id: str
    clip_id: str
    workflow: str
    project_input_revision: str
    clip_input_revision: str
    trajectory_output_revision: str
    trajectory_output_fingerprint: str
    camera_track: dict[str, object]
    promoted_to_workbench_output_revision: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "operation_id": self.operation_id,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "workflow": self.workflow,
            "project_input_revision": self.project_input_revision,
            "clip_input_revision": self.clip_input_revision,
            "trajectory_output_revision": self.trajectory_output_revision,
            "trajectory_output_fingerprint": self.trajectory_output_fingerprint,
            "camera_track": self.camera_track,
            "promoted_to_workbench_output_revision": (
                self.promoted_to_workbench_output_revision
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkbenchDraft:
        camera_track = value["camera_track"]
        if not isinstance(camera_track, Mapping):
            raise TypeError("camera_track must be an object")
        return cls(
            schema_version=str(value["schema_version"]),
            revision=int(value["revision"]),
            operation_id=str(value["operation_id"]),
            updated_at=str(value["updated_at"]),
            project_id=str(value["project_id"]),
            clip_id=str(value["clip_id"]),
            workflow=str(value["workflow"]),
            project_input_revision=str(value["project_input_revision"]),
            clip_input_revision=str(value["clip_input_revision"]),
            trajectory_output_revision=str(value["trajectory_output_revision"]),
            trajectory_output_fingerprint=str(value["trajectory_output_fingerprint"]),
            camera_track=dict(camera_track),
            promoted_to_workbench_output_revision=_optional_string(
                value.get("promoted_to_workbench_output_revision")
            ),
        )


class AtomicWorkbenchDraftStore:
    """保存用户明确确认过的关键帧草稿，不复制任何大型输入或派生产物。"""

    def __init__(
        self, root: Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self.root = root
        self.now = now or (lambda: datetime.now(timezone.utc))

    def path_for(self, project_id: str, clip_id: str) -> Path:
        project_id = validate_project_id(project_id)
        if not is_safe_stable_id(clip_id):
            raise ValueError("invalid clip_id")
        return self.root / project_id / "workbench_drafts" / f"{clip_id}.json"

    def load_optional(self, project_id: str, clip_id: str) -> WorkbenchDraft | None:
        path = self.path_for(project_id, clip_id)
        with self._lock_for(path):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    raise TypeError("draft record must be an object")
                draft = WorkbenchDraft.from_dict(payload)
                self._validate_identity(project_id, clip_id, draft)
                return draft
            except (
                FileNotFoundError,
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return None

    def load_compatible(
        self,
        project_id: str,
        clip_id: str,
        *,
        workflow: str,
        project_input_revision: str,
        clip_input_revision: str,
        trajectory_output_revision: str,
        trajectory_output_fingerprint: str,
    ) -> WorkbenchDraft | None:
        draft = self.load_optional(project_id, clip_id)
        if draft is None or draft.promoted_to_workbench_output_revision is not None:
            return None
        binding = (
            workflow,
            project_input_revision,
            clip_input_revision,
            trajectory_output_revision,
            trajectory_output_fingerprint,
        )
        stored = (
            draft.workflow,
            draft.project_input_revision,
            draft.clip_input_revision,
            draft.trajectory_output_revision,
            draft.trajectory_output_fingerprint,
        )
        return draft if stored == binding else None

    def update(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int | None,
        operation_id: str,
        workflow: str,
        project_input_revision: str,
        clip_input_revision: str,
        trajectory_output_revision: str,
        trajectory_output_fingerprint: str,
        camera_track: Mapping[str, object],
    ) -> WorkbenchDraft:
        path = self.path_for(project_id, clip_id)
        with self._lock_for(path):
            current = self.load_optional(project_id, clip_id)
            current_revision = None if current is None else current.revision
            if current_revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=-1 if expected_revision is None else expected_revision,
                    current_revision=-1 if current_revision is None else current_revision,
                )
            draft = WorkbenchDraft(
                schema_version=DRAFT_SCHEMA_VERSION,
                revision=0 if current is None else current.revision + 1,
                operation_id=_required_string(operation_id, "operation_id"),
                updated_at=_timestamp(self.now()),
                project_id=validate_project_id(project_id),
                clip_id=clip_id,
                workflow=_required_string(workflow, "workflow"),
                project_input_revision=_required_string(
                    project_input_revision, "project_input_revision"
                ),
                clip_input_revision=_required_string(
                    clip_input_revision, "clip_input_revision"
                ),
                trajectory_output_revision=_required_string(
                    trajectory_output_revision, "trajectory_output_revision"
                ),
                trajectory_output_fingerprint=_required_string(
                    trajectory_output_fingerprint, "trajectory_output_fingerprint"
                ),
                camera_track=dict(camera_track),
            )
            self._validate_identity(project_id, clip_id, draft)
            self._atomic_write(path, draft)
            return draft

    def mark_promoted(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        workbench_output_revision: str,
    ) -> WorkbenchDraft:
        path = self.path_for(project_id, clip_id)
        with self._lock_for(path):
            current = self.load_optional(project_id, clip_id)
            if current is None:
                raise FileNotFoundError(path)
            if current.revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=expected_revision,
                    current_revision=current.revision,
                )
            promoted = replace(
                current,
                revision=current.revision + 1,
                operation_id=_required_string(operation_id, "operation_id"),
                updated_at=_timestamp(self.now()),
                promoted_to_workbench_output_revision=_required_string(
                    workbench_output_revision, "workbench_output_revision"
                ),
            )
            self._atomic_write(path, promoted)
            return promoted

    @staticmethod
    def _lock_for(path: Path) -> RLock:
        key = os.path.normcase(str(path.resolve(strict=False)))
        with _LOCKS_GUARD:
            return _RECORD_LOCKS.setdefault(key, RLock())

    @staticmethod
    def _validate_identity(
        project_id: str, clip_id: str, draft: WorkbenchDraft
    ) -> None:
        if draft.schema_version != DRAFT_SCHEMA_VERSION:
            raise ValueError("unsupported workbench draft schema")
        if draft.project_id != validate_project_id(project_id):
            raise ValueError("workbench draft project mismatch")
        if not is_safe_stable_id(clip_id) or draft.clip_id != clip_id:
            raise ValueError("workbench draft clip mismatch")
        if draft.revision < 0:
            raise ValueError("workbench draft revision must be non-negative")
        for value, label in (
            (draft.operation_id, "operation_id"),
            (draft.updated_at, "updated_at"),
            (draft.workflow, "workflow"),
            (draft.project_input_revision, "project_input_revision"),
            (draft.clip_input_revision, "clip_input_revision"),
            (draft.trajectory_output_revision, "trajectory_output_revision"),
            (draft.trajectory_output_fingerprint, "trajectory_output_fingerprint"),
        ):
            _required_string(value, label)
        if not isinstance(draft.camera_track, dict):
            raise TypeError("camera_track must be an object")

    @staticmethod
    def _atomic_write(path: Path, draft: WorkbenchDraft) -> None:
        payload = (
            json.dumps(draft.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _required_string(value, "revision")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
