from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from .io import read_json, write_json
from .manifest import RunManifest, StageRecord


class ArtifactManager:
    """统一管理 `runs/<dataset>/<run_id>/` 输出和 manifest。"""

    def __init__(self, output_root: str | Path = "runs", dataset: str = "default", run_id: str = "default") -> None:
        self.output_root = Path(output_root)
        self.dataset = dataset
        self.run_id = run_id
        self.run_dir = self.output_root / self.dataset / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.run_dir / "manifest.json"
        self.manifest = self._load_or_create_manifest()

    def _load_or_create_manifest(self) -> RunManifest:
        if self.manifest_path.exists():
            return RunManifest.from_dict(read_json(self.manifest_path))
        manifest = RunManifest(dataset=self.dataset, run_id=self.run_id, output_root=self.output_root)
        write_json(self.manifest_path, manifest.to_dict())
        return manifest

    def stage_dir(self, stage_name: str, output_subdir: str) -> Path:
        if Path(output_subdir).is_absolute() or ".." in Path(output_subdir).parts:
            raise ValueError("output_subdir 必须位于当前 run 目录下。")
        path = self.run_dir / output_subdir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, path: str | Path, data) -> Path:
        out = Path(path)
        resolved = out.resolve()
        if self.run_dir.resolve() not in (resolved, *resolved.parents):
            raise ValueError("ArtifactManager 只能写入当前 run 目录。")
        return write_json(out, data)

    def record_stage(
        self,
        *,
        stage_name: str,
        command: Sequence[str],
        inputs: Mapping[str, object],
        outputs: Mapping[str, object],
        metrics: Mapping[str, object] | None = None,
        status: str = "success",
    ) -> None:
        self.manifest.add_stage(
            StageRecord(
                stage_name=stage_name,
                command=command,
                inputs=inputs,
                outputs=outputs,
                metrics=metrics or {},
                status=status,
            )
        )
        write_json(self.manifest_path, self.manifest.to_dict())

