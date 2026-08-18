"""Project visual annotations and immutable tracking artifacts."""

from .models import (
    Annotation,
    AnnotationStyle,
    AnnotationsManifest,
    SourcePtsRange,
    VisibilityPolicy,
)
from .service import (
    AnnotationDeleteResult,
    AnnotationMutationResult,
    AnnotationRevisionConflict,
    AnnotationService,
)
from .cad_anchor import CadAnchorProjection, project_cad_anchor

__all__ = [
    "Annotation",
    "AnnotationStyle",
    "AnnotationsManifest",
    "SourcePtsRange",
    "VisibilityPolicy",
    "AnnotationDeleteResult",
    "AnnotationMutationResult",
    "AnnotationRevisionConflict",
    "AnnotationService",
    "CadAnchorProjection",
    "project_cad_anchor",
]
