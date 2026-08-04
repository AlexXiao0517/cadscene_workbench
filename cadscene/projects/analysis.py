from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
from typing import Callable, Mapping
from uuid import uuid4

from cadscene.video_analysis.analyzer import analyze_video
from cadscene.workflow.data_import import import_cad

from .json_repositories import ProjectRepositories
from .models import ClipDefinition, ProjectManifest, register_analysis_revision
from .repositories import ManifestMutation, publish_manifests
from .uploads import PublishedUpload


class ProjectAnalysisCoordinator:
    """Runs bounded light analysis after, and only after, validated publication."""

    def __init__(
        self,
        repositories: ProjectRepositories,
        *,
        projects_root: Path,
        storage_root: Path,
        now: Callable[[], str],
        identity: Callable[[], str] | None = None,
        video_analyzer: Callable[..., Path] = analyze_video,
        cad_importer: Callable[..., Mapping[str, object]] = import_cad,
        max_workers: int = 1,
    ) -> None:
        if max_workers < 1:
            raise ValueError("analysis max_workers must be positive")
        self.repositories = repositories
        self.projects_root = Path(projects_root)
        self.storage_root = Path(storage_root)
        self.now = now
        self._identity = identity or (lambda: f"analysis-{uuid4().hex}")
        self._video_analyzer = video_analyzer
        self._cad_importer = cad_importer
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="project-analysis"
        )

    def trigger(
        self, project_id: str, asset_type: str, upload: PublishedUpload
    ) -> Future[None]:
        if upload.project_id != project_id:
            raise ValueError("published upload belongs to another project")
        if asset_type in {"video", "cad", "srt"}:
            request_key = _analysis_request_key(
                self.repositories.project.load(project_id).source_assets
            )
            return self._executor.submit(
                self._run_ready_project, project_id, upload, request_key
            )
        raise ValueError(f"unsupported analysis asset type: {asset_type}")

    def _run_ready_project(
        self,
        project_id: str,
        triggering_upload: PublishedUpload,
        request_key: str | None,
    ) -> None:
        project = self.repositories.project.load(project_id)
        if _analysis_request_key(project.source_assets) != request_key:
            return
        cad_asset = project.source_assets.get("cad")
        if isinstance(cad_asset, Mapping) and not cad_asset.get("analysis"):
            cad_path = _asset_path(project.source_assets, "cad")
            if cad_path is None or not cad_path.is_file():
                raise FileNotFoundError("published project CAD is unavailable")
            cad_upload = (
                triggering_upload
                if triggering_upload.asset_type == "cad"
                else PublishedUpload(
                    project_id=project_id,
                    asset_type="cad",
                    original_filename=str(
                        cad_asset.get("original_filename") or cad_path.name
                    ),
                    path=cad_path,
                    size_bytes=int(cad_asset.get("size_bytes", cad_path.stat().st_size)),
                    sha256=str(cad_asset.get("sha256", "")),
                    validation={},
                    validation_report_path=Path(
                        str(cad_asset.get("validation_report", ""))
                    ),
                )
            )
            self._run_cad(project_id, cad_upload, request_key=request_key)
            if (
                _analysis_request_key(
                    self.repositories.project.load(project_id).source_assets
                )
                != request_key
            ):
                return
        self._run_video(project_id, request_key=request_key)

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run_cad(
        self,
        project_id: str,
        upload: PublishedUpload,
        *,
        request_key: str | None,
    ) -> None:
        try:
            with upload.path.open("rb") as stream:
                result = dict(
                    self._cad_importer(
                        self.storage_root,
                        project_id,
                        upload.original_filename,
                        stream,
                    )
                )
            with self.repositories.project.lock_for(project_id):
                current = self.repositories.project.load(project_id)
                if _analysis_request_key(current.source_assets) != request_key:
                    return
                assets = dict(current.source_assets)
                cad = dict(assets.get("cad", {}))
                cad["analysis"] = result.get("cad", result)
                assets["cad"] = cad
                analysis = dict(assets.get("_analysis", {}))
                analysis["status"] = "running"
                assets["_analysis"] = analysis
                self.repositories.project.update(
                    project_id,
                    expected_revision=current.revision,
                    mutate=lambda value: replace(
                        value,
                        updated_at=self.now(),
                        source_assets=assets,
                        project_state="analyzing",
                    ),
                )
        except Exception as exc:
            self._record_failure(project_id, "cad", exc, request_key=request_key)
            raise

    def _run_video(self, project_id: str, *, request_key: str | None) -> None:
        try:
            project = self.repositories.project.load(project_id)
            video = _asset_path(project.source_assets, "video")
            if video is None or not video.is_file():
                raise FileNotFoundError("published project video is unavailable")
            srt = _asset_path(project.source_assets, "srt")
            revision = self._identity()
            attempt_root = (
                self.projects_root
                / project_id
                / ".analysis_attempts"
                / f"{revision}-{uuid4().hex}"
            )
            attempt_root.mkdir(parents=True, exist_ok=False)
            output = self._video_analyzer(
                video_path=video,
                output_root=attempt_root,
                project_id=project_id,
                srt_path=srt if srt is not None and srt.is_file() else None,
                analysis_revision=revision,
            )
            clip_manifest = json.loads(
                (Path(output) / "clip_manifest.json").read_text(encoding="utf-8")
            )
            if str(clip_manifest.get("analysis_revision")) != revision:
                raise ValueError("analysis artifact revision does not match request")
            clips = tuple(
                ClipDefinition.from_analysis(
                    item,
                    generated_display_name=(
                        f"场景 {int(item.get('scene_index', 1)):02d} · "
                        f"第 {int(item.get('segment_index', 1))} 段"
                    ),
                )
                for item in clip_manifest.get("clips", ())
            )
            if not clips:
                raise ValueError("video analysis produced no logical clips")
            if _analysis_request_key(
                self.repositories.project.load(project_id).source_assets
            ) != request_key:
                return
            self._publish_analysis_artifacts(
                project_id, revision, Path(output), attempt_root
            )
            self._publish_analysis(
                project_id, revision, clips, request_key=request_key
            )
        except Exception as exc:
            self._record_failure(project_id, "video", exc, request_key=request_key)
            raise

    def _publish_analysis_artifacts(
        self,
        project_id: str,
        revision: str,
        output: Path,
        attempt_root: Path,
    ) -> Path:
        analyses = self.projects_root / project_id / "analyses"
        analyses.mkdir(parents=True, exist_ok=True)
        target = analyses / revision
        if target.exists():
            raise FileExistsError(
                f"immutable analysis revision already exists: {revision}"
            )
        staging = analyses / f".{revision}.tmp-{uuid4().hex}"
        staging.mkdir(exist_ok=False)
        try:
            os.replace(output, staging / "02_video_analysis")
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if attempt_root.exists():
            shutil.rmtree(attempt_root)
        return target

    def _publish_analysis(
        self,
        project_id: str,
        revision: str,
        clips: tuple[ClipDefinition, ...],
        *,
        request_key: str | None,
    ) -> None:
        with self.repositories.project.lock_for(
            project_id
        ), self.repositories.clips.lock_for(project_id):
            project = self.repositories.project.load(project_id)
            if _analysis_request_key(project.source_assets) != request_key:
                return
            active_clips = self.repositories.clips.load(project_id)

            def mutate_project(
                value: ProjectManifest, operation_id: str
            ) -> ProjectManifest:
                assets = dict(value.source_assets)
                analysis = dict(assets.get("_analysis", {}))
                analysis.update(
                    {"status": "success", "analysis_revision": revision}
                )
                assets["_analysis"] = analysis
                return replace(
                    register_analysis_revision(
                        value, revision, operation_id=operation_id
                    ),
                    updated_at=self.now(),
                    source_assets=assets,
                    project_state=(
                        "ready"
                        if value.active_analysis_revision is None
                        else "analysis_candidate_ready"
                    ),
                )

            mutations = [
                ManifestMutation(
                    repository=self.repositories.project,
                    project_id=project_id,
                    expected_revision=project.revision,
                    mutate=mutate_project,
                )
            ]
            if project.active_analysis_revision is None:
                mutations.append(
                    ManifestMutation(
                        repository=self.repositories.clips,
                        project_id=project_id,
                        expected_revision=active_clips.revision,
                        mutate=lambda value, _operation_id: replace(
                            value,
                            updated_at=self.now(),
                            analysis_revision=revision,
                            clips=clips,
                        ),
                    )
                )
            publish_manifests(mutations)

    def _record_failure(
        self,
        project_id: str,
        kind: str,
        error: Exception,
        *,
        request_key: str | None,
    ) -> None:
        with self.repositories.project.lock_for(project_id):
            current = self.repositories.project.load(project_id)
            if _analysis_request_key(current.source_assets) != request_key:
                return
            assets = dict(current.source_assets)
            asset = dict(assets.get(kind, {}))
            asset["analysis_status"] = "failed"
            asset["analysis_error"] = str(error)
            assets[kind] = asset
            analysis = dict(assets.get("_analysis", {}))
            analysis.update({"status": "failed", "error": str(error)})
            assets["_analysis"] = analysis
            self.repositories.project.update(
                project_id,
                expected_revision=current.revision,
                mutate=lambda value: replace(
                    value,
                    updated_at=self.now(),
                    source_assets=assets,
                    project_state="analysis_failed",
                ),
            )


def _asset_path(assets: Mapping[str, object], name: str) -> Path | None:
    value = assets.get(name) or assets.get(f"{name}_path")
    if isinstance(value, Mapping):
        value = value.get("path")
    return None if value in (None, "") else Path(str(value))


def _analysis_request_key(assets: Mapping[str, object]) -> str | None:
    analysis = assets.get("_analysis")
    if not isinstance(analysis, Mapping):
        return None
    value = analysis.get("request_key")
    return None if value in (None, "") else str(value)
