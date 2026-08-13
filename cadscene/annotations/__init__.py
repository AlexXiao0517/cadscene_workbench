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
]
