from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _stringify_paths(data: Mapping[str, object]) -> dict:
    out: dict[str, object] = {}
    for key, value in data.items():
        out[key] = str(value) if isinstance(value, Path) else value
    return out


@dataclass
class StageRecord:
    stage_name: str
    command: Sequence[str]
    inputs: Mapping[str, object] = field(default_factory=dict)
    outputs: Mapping[str, object] = field(default_factory=dict)
    metrics: Mapping[str, object] = field(default_factory=dict)
    status: str = "success"
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "stage_name": self.stage_name,
            "command": [str(item) for item in self.command],
            "inputs": _stringify_paths(self.inputs),
            "outputs": _stringify_paths(self.outputs),
            "metrics": dict(self.metrics),
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass
class RunManifest:
    dataset: str
    run_id: str
    output_root: Path
    schema_version: str = "1.0"
    project_version: str = "v0.1-sfm-workbench"
    created_at: str = field(default_factory=now_iso)
    stages: list[StageRecord] = field(default_factory=list)

    def add_stage(self, record: StageRecord) -> None:
        self.stages = [stage for stage in self.stages if stage.stage_name != record.stage_name]
        self.stages.append(record)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project_version": self.project_version,
            "dataset": self.dataset,
            "run_id": self.run_id,
            "output_root": str(self.output_root),
            "created_at": self.created_at,
            "stages": [stage.to_dict() for stage in self.stages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunManifest":
        manifest = cls(
            dataset=str(data["dataset"]),
            run_id=str(data["run_id"]),
            output_root=Path(data.get("output_root", "runs")),
            schema_version=str(data.get("schema_version", "1.0")),
            project_version=str(data.get("project_version", "v0.1-sfm-workbench")),
            created_at=str(data.get("created_at", now_iso())),
        )
        for stage in data.get("stages", []):
            manifest.stages.append(
                StageRecord(
                    stage_name=str(stage["stage_name"]),
                    command=[str(item) for item in stage.get("command", [])],
                    inputs=stage.get("inputs", {}),
                    outputs=stage.get("outputs", {}),
                    metrics=stage.get("metrics", {}),
                    status=str(stage.get("status", "success")),
                    created_at=str(stage.get("created_at", now_iso())),
                )
            )
        return manifest

