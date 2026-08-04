from __future__ import annotations

from .service import EnqueueAnalysisResult, ProjectService
from .uploads import PublishedUpload


class ProjectAnalysisCoordinator:
    """Compatibility facade for durable queue-backed project analysis.

    Analysis execution is owned by ``LocalResourceQueue`` and
    ``LocalJobExecutor``.  This facade deliberately owns no thread, Future or
    publication code; callers receive the persisted DAG identity immediately.
    """

    def __init__(self, service: ProjectService) -> None:
        self.service = service

    def trigger(
        self, project_id: str, asset_type: str, upload: PublishedUpload
    ) -> EnqueueAnalysisResult:
        if upload.project_id != project_id:
            raise ValueError("published upload belongs to another project")
        if upload.asset_type != asset_type:
            raise ValueError("published upload type does not match trigger")
        if asset_type not in {"video", "cad", "srt"}:
            raise ValueError(f"unsupported analysis asset type: {asset_type}")
        return self.service.enqueue_analysis_jobs(project_id)

    def close(self, *, wait: bool = True) -> None:
        del wait
