from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping

from cadscene.workflow.data_import import load_dataset_manifest

from .analysis_adapters import tree_fingerprint


@dataclass(frozen=True)
class PublishedAnalysisArtifacts:
    cad_dataset_id: str
    cad_dataset_path: Path
    cad_manifest: Mapping[str, object]
    analysis_artifact_id: str
    analysis_artifact_path: Path


@dataclass(frozen=True)
class PublishedCadArtifact:
    dataset_id: str
    dataset_path: Path
    manifest: Mapping[str, object]


class AnalysisArtifactPublisher:
    """Publishes validated trees by content identity, never by mutable revision."""

    def __init__(
        self,
        *,
        storage_root: Path,
        projects_root: Path,
        identity: Callable[[], str],
    ) -> None:
        self.storage_root = Path(storage_root)
        self.projects_root = Path(projects_root)
        self.identity = identity

    def publish(
        self,
        *,
        project_id: str,
        cad_source: Path,
        cad_fingerprint: str,
        analysis_source: Path,
        analysis_fingerprint: str,
    ) -> PublishedAnalysisArtifacts:
        _require_fingerprint(
            cad_source, cad_fingerprint, label="validated CAD output"
        )
        _require_fingerprint(
            analysis_source,
            analysis_fingerprint,
            label="validated video-analysis output",
        )
        cad_dataset_id = _content_id("cad", cad_fingerprint)
        analysis_artifact_id = _content_id("video-analysis", analysis_fingerprint)
        cad_target = self.storage_root / "data" / cad_dataset_id
        analysis_target = (
            self.projects_root
            / project_id
            / "analysis_artifacts"
            / analysis_artifact_id
        )
        self._publish_cad_dataset(
            cad_source,
            cad_target,
            dataset_id=cad_dataset_id,
            source_fingerprint=cad_fingerprint,
        )
        wrapper = analysis_source.parent / f".publish-{self.identity()}"
        (wrapper / "02_video_analysis").parent.mkdir(
            parents=True, exist_ok=False
        )
        shutil.copytree(analysis_source, wrapper / "02_video_analysis")
        try:
            _require_fingerprint(
                wrapper / "02_video_analysis",
                analysis_fingerprint,
                label="copied video-analysis output",
            )
            self._publish_immutable_tree(wrapper, analysis_target)
        finally:
            if wrapper.exists():
                shutil.rmtree(wrapper)
        cad_manifest = load_dataset_manifest(self.storage_root, cad_dataset_id)
        return PublishedAnalysisArtifacts(
            cad_dataset_id=cad_dataset_id,
            cad_dataset_path=cad_target,
            cad_manifest=cad_manifest,
            analysis_artifact_id=analysis_artifact_id,
            analysis_artifact_path=analysis_target,
        )

    def publish_cad(
        self,
        *,
        cad_source: Path,
        cad_fingerprint: str,
    ) -> PublishedCadArtifact:
        """发布单份不可变 CAD 数据集，不触碰视频分析产物。"""

        _require_fingerprint(
            cad_source, cad_fingerprint, label="validated CAD output"
        )
        dataset_id = _content_id("cad", cad_fingerprint)
        target = self.storage_root / "data" / dataset_id
        self._publish_cad_dataset(
            cad_source,
            target,
            dataset_id=dataset_id,
            source_fingerprint=cad_fingerprint,
        )
        return PublishedCadArtifact(
            dataset_id=dataset_id,
            dataset_path=target,
            manifest=load_dataset_manifest(self.storage_root, dataset_id),
        )

    def _publish_immutable_tree(self, source: Path, target: Path) -> None:
        expected_tree_fingerprint = tree_fingerprint(source)
        if target.exists():
            if (
                not target.is_dir()
                or expected_tree_fingerprint != tree_fingerprint(target)
            ):
                raise FileExistsError(
                    f"immutable output exists with different content: {target}"
                )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{target.name}.tmp-{self.identity()}"
        shutil.copytree(source, staging)
        try:
            _require_fingerprint(
                staging,
                expected_tree_fingerprint,
                label="copied normalized analysis artifact",
            )
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _publish_cad_dataset(
        self,
        source: Path,
        target: Path,
        *,
        dataset_id: str,
        source_fingerprint: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_root = self.storage_root / f".cad-publish-{self.identity()}"
        staging = staging_root / "data" / dataset_id
        shutil.copytree(source, staging)
        try:
            _require_fingerprint(
                staging,
                source_fingerprint,
                label="copied CAD output",
            )
            manifest_path = staging / "dataset_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            old_dataset = str(manifest.get("dataset") or source.name)
            rewritten = rewrite_dataset_references(
                manifest, old_dataset=old_dataset, dataset_id=dataset_id
            )
            if not isinstance(rewritten, dict):
                raise ValueError("rewritten CAD manifest must be an object")
            rewritten["dataset"] = dataset_id
            manifest_path.write_text(
                json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            cad = rewritten.get("cad")
            if not isinstance(cad, Mapping):
                raise ValueError("published CAD manifest has no cad section")
            design_value = cad.get("design_json")
            if design_value:
                relative = Path(str(design_value))
                try:
                    within_dataset = relative.relative_to(
                        Path("data") / dataset_id
                    )
                except ValueError as exc:
                    raise ValueError(
                        "published CAD design path does not use content ID"
                    ) from exc
                if not (staging / within_dataset).is_file():
                    raise FileNotFoundError("published CAD design file is missing")
            normalized = load_dataset_manifest(staging_root, dataset_id)
            if str(normalized.get("dataset")) != dataset_id:
                raise ValueError("staged CAD dataset identity failed validation")
            normalized_fingerprint = tree_fingerprint(staging)
            if target.exists():
                if (
                    not target.is_dir()
                    or normalized_fingerprint != tree_fingerprint(target)
                ):
                    raise FileExistsError(
                        "immutable CAD dataset exists with different content: "
                        f"{target}"
                    )
                self._validate_cad_dataset(target, dataset_id=dataset_id)
                return
            _require_fingerprint(
                staging,
                normalized_fingerprint,
                label="normalized CAD publication staging",
            )
            os.replace(staging, target)
            self._validate_cad_dataset(target, dataset_id=dataset_id)
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)

    def _validate_cad_dataset(self, target: Path, *, dataset_id: str) -> None:
        if not target.is_dir():
            raise FileNotFoundError("published CAD dataset directory is missing")
        loaded = load_dataset_manifest(self.storage_root, dataset_id)
        if str(loaded.get("dataset")) != dataset_id:
            raise ValueError("published CAD dataset identity failed validation")
        cad = loaded.get("cad")
        if not isinstance(cad, Mapping):
            raise ValueError("published CAD manifest has no cad section")
        design_value = cad.get("design_json")
        if design_value and not (self.storage_root / str(design_value)).is_file():
            raise FileNotFoundError("published CAD design file is missing")


def rewrite_dataset_references(
    value: object, *, old_dataset: str, dataset_id: str
) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): rewrite_dataset_references(
                item, old_dataset=old_dataset, dataset_id=dataset_id
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            rewrite_dataset_references(
                item, old_dataset=old_dataset, dataset_id=dataset_id
            )
            for item in value
        ]
    if isinstance(value, str):
        return value.replace(
            f"data/{old_dataset}/", f"data/{dataset_id}/"
        ).replace(f"/data/{old_dataset}/", f"/data/{dataset_id}/")
    return value


def _content_id(prefix: str, fingerprint: str) -> str:
    normalized = str(fingerprint).lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError(f"{prefix} fingerprint must be a SHA-256 digest")
    return f"{prefix}-{normalized}"


def _require_fingerprint(
    source: Path, expected: str, *, label: str
) -> None:
    normalized = str(expected).lower()
    _content_id(label, normalized)
    actual = tree_fingerprint(source)
    if actual != normalized:
        raise ValueError(
            f"{label} fingerprint mismatch: expected {normalized}, got {actual}"
        )
