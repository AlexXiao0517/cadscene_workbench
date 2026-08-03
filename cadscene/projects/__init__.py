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
]
