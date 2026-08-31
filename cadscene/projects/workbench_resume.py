from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Lock, RLock
from typing import Callable, Mapping

from .identifiers import is_safe_stable_id, validate_project_id
from .repositories import RevisionConflict


RESUME_SCHEMA_VERSION = "1.0"
_ALLOWED_STAGES = frozenset({"sfm", "keyframes", "quality", "render"})
_LOCKS_GUARD = Lock()
_RECORD_LOCKS: dict[str, RLock] = {}


@dataclass(frozen=True)
class WorkbenchResumeState:
    schema_version: str
    revision: int
    operation_id: str
    updated_at: str
    project_id: str
    clip_id: str
    workflow_stage: str
    source_pts: int
    source_time_base: dict[str, int]
    trajectory_output_revision: str | None
    workbench_output_revision: str | None
    quality_revision: str | None
    render_revision: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "operation_id": self.operation_id,
            "updated_at": self.updated_at,
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "workflow_stage": self.workflow_stage,
            "source_pts": self.source_pts,
            "source_time_base": dict(self.source_time_base),
            "trajectory_output_revision": self.trajectory_output_revision,
            "workbench_output_revision": self.workbench_output_revision,
            "quality_revision": self.quality_revision,
            "render_revision": self.render_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> WorkbenchResumeState:
        time_base = value["source_time_base"]
        if not isinstance(time_base, Mapping):
            raise TypeError("source_time_base must be an object")
        return cls(
            schema_version=str(value["schema_version"]),
            revision=int(value["revision"]),
            operation_id=str(value["operation_id"]),
            updated_at=str(value["updated_at"]),
            project_id=str(value["project_id"]),
            clip_id=str(value["clip_id"]),
            workflow_stage=str(value["workflow_stage"]),
            source_pts=_strict_integer(value["source_pts"], "source_pts"),
            source_time_base=_normalize_time_base(time_base),
            trajectory_output_revision=_optional_string(
                value.get("trajectory_output_revision")
            ),
            workbench_output_revision=_optional_string(
                value.get("workbench_output_revision")
            ),
            quality_revision=_optional_string(value.get("quality_revision")),
            render_revision=_optional_string(value.get("render_revision")),
        )


class AtomicWorkbenchResumeStore:
    """每个片段独立修订的导航恢复记录，不复制业务产物。"""

    def __init__(
        self, root: Path, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self.root = root
        self.now = now or (lambda: datetime.now(timezone.utc))

    def path_for(self, project_id: str, clip_id: str) -> Path:
        project_id = validate_project_id(project_id)
        if not is_safe_stable_id(clip_id):
            raise ValueError("invalid clip_id")
        return self.root / project_id / "workbench_resume" / f"{clip_id}.json"

    def load_optional(
        self, project_id: str, clip_id: str
    ) -> WorkbenchResumeState | None:
        path = self.path_for(project_id, clip_id)
        with self._lock_for(path):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    payload = json.load(stream)
                if not isinstance(payload, Mapping):
                    raise TypeError("resume record must be an object")
                state = WorkbenchResumeState.from_dict(payload)
                self._validate_identity(project_id, clip_id, state)
                return state
            except (
                FileNotFoundError,
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return None

    def update(
        self,
        project_id: str,
        clip_id: str,
        *,
        expected_revision: int | None,
        operation_id: str,
        workflow_stage: str,
        source_pts: int,
        source_time_base: Mapping[str, object],
        trajectory_output_revision: str | None,
        workbench_output_revision: str | None,
        quality_revision: str | None,
        render_revision: str | None,
    ) -> WorkbenchResumeState:
        path = self.path_for(project_id, clip_id)
        with self._lock_for(path):
            current = self.load_optional(project_id, clip_id)
            current_revision = None if current is None else current.revision
            if current_revision != expected_revision:
                raise RevisionConflict(
                    project_id=project_id,
                    expected_revision=(
                        -1 if expected_revision is None else expected_revision
                    ),
                    current_revision=(
                        -1 if current_revision is None else current_revision
                    ),
                )
            state = WorkbenchResumeState(
                schema_version=RESUME_SCHEMA_VERSION,
                revision=0 if current is None else current.revision + 1,
                operation_id=_required_string(operation_id, "operation_id"),
                updated_at=_timestamp(self.now()),
                project_id=validate_project_id(project_id),
                clip_id=clip_id,
                workflow_stage=_validate_stage(workflow_stage),
                source_pts=_strict_integer(source_pts, "source_pts"),
                source_time_base=_normalize_time_base(source_time_base),
                trajectory_output_revision=_optional_string(
                    trajectory_output_revision
                ),
                workbench_output_revision=_optional_string(
                    workbench_output_revision
                ),
                quality_revision=_optional_string(quality_revision),
                render_revision=_optional_string(render_revision),
            )
            self._validate_identity(project_id, clip_id, state)
            self._atomic_write(path, state)
            return state

    @staticmethod
    def _lock_for(path: Path) -> RLock:
        key = os.path.normcase(str(path.resolve(strict=False)))
        with _LOCKS_GUARD:
            return _RECORD_LOCKS.setdefault(key, RLock())

    @staticmethod
    def _validate_identity(
        project_id: str, clip_id: str, state: WorkbenchResumeState
    ) -> None:
        if state.schema_version != RESUME_SCHEMA_VERSION:
            raise ValueError("unsupported workbench resume schema")
        if state.project_id != validate_project_id(project_id):
            raise ValueError("workbench resume project mismatch")
        if not is_safe_stable_id(clip_id) or state.clip_id != clip_id:
            raise ValueError("workbench resume clip mismatch")
        if state.revision < 0:
            raise ValueError("workbench resume revision must be non-negative")
        _required_string(state.operation_id, "operation_id")
        _required_string(state.updated_at, "updated_at")
        _validate_stage(state.workflow_stage)
        _strict_integer(state.source_pts, "source_pts")
        _normalize_time_base(state.source_time_base)

    @staticmethod
    def _atomic_write(path: Path, state: WorkbenchResumeState) -> None:
        serialized = (
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
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
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _strict_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} must be an integer")
    return value


def _normalize_time_base(value: Mapping[str, object]) -> dict[str, int]:
    numerator = _strict_integer(value.get("numerator"), "time_base numerator")
    denominator = _strict_integer(
        value.get("denominator"), "time_base denominator"
    )
    if numerator <= 0 or denominator <= 0:
        raise ValueError("source_time_base values must be positive")
    return {"numerator": numerator, "denominator": denominator}


def _validate_stage(value: object) -> str:
    stage = _required_string(value, "workflow_stage")
    if stage not in _ALLOWED_STAGES:
        raise ValueError(f"unsupported workflow_stage: {stage}")
    return stage


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
