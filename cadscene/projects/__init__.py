"""Project manifests, persistence boundaries, and consistency recovery."""

from .models import (
    ClipDefinition,
    ClipsManifest,
    ClipWorkflow,
    JobsManifest,
    ManifestHeader,
    OperationIntent,
    ProjectManifest,
    RenderManifest,
    StateReference,
    activate_analysis_revision,
    reconcile_clips,
    register_analysis_revision,
)
from .repositories import ManifestRepository, RevisionConflict
from .json_repositories import AtomicJsonRepository, project_repositories
from .recovery import reconcile_project
from .adapters import (
    AdapterProgress,
    AdapterResult,
    WorkflowAdapter,
    WorkflowAdapterRegistry,
)
from .queue import AttemptRecord, LocalResourceQueue, QueueJob, TaskQueue
from .service import ProjectService
from .executor import JobExecutionPlan, LocalJobExecutor
from .uploads import PublishedUpload, UploadValidationError, ValidatedUploadStore
from .http_api import ApiResponse, ProjectApi, UploadRequest
from .analysis import ProjectAnalysisCoordinator

__all__ = [
    "ClipDefinition",
    "ClipsManifest",
    "ClipWorkflow",
    "JobsManifest",
    "ManifestHeader",
    "OperationIntent",
    "ProjectManifest",
    "RenderManifest",
    "StateReference",
    "activate_analysis_revision",
    "reconcile_clips",
    "register_analysis_revision",
    "ManifestRepository",
    "RevisionConflict",
    "AtomicJsonRepository",
    "project_repositories",
    "reconcile_project",
    "AdapterProgress",
    "AdapterResult",
    "WorkflowAdapter",
    "WorkflowAdapterRegistry",
    "AttemptRecord",
    "LocalResourceQueue",
    "QueueJob",
    "TaskQueue",
    "ProjectService",
    "JobExecutionPlan",
    "LocalJobExecutor",
    "PublishedUpload",
    "UploadValidationError",
    "ValidatedUploadStore",
    "ApiResponse",
    "ProjectApi",
    "UploadRequest",
    "ProjectAnalysisCoordinator",
]
