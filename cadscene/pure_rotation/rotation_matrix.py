from __future__ import annotations

from typing import Any, Iterable, Mapping

import numpy as np


def coerce_so3_matrix(value: Any, *, allow_legacy_reflection: bool = False) -> np.ndarray:
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise ValueError("rotation must be a finite 3x3 matrix")
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6):
        raise ValueError("rotation matrix must be orthogonal")
    determinant = float(np.linalg.det(matrix))
    if np.isclose(determinant, 1.0, atol=1e-6):
        return matrix
    if allow_legacy_reflection and np.isclose(determinant, -1.0, atol=1e-6):
        migrated = matrix.copy()
        migrated[:, 0] *= -1.0
        return migrated
    raise ValueError("rotation matrix determinant must be +1")


def normalize_rotation_fields(
    payload: Mapping[str, Any],
    fields: Iterable[str],
    *,
    allow_legacy_reflection: bool = False,
) -> dict[str, Any]:
    normalized = dict(payload)
    for field in fields:
        if field in normalized and normalized[field] is not None:
            normalized[field] = coerce_so3_matrix(
                normalized[field],
                allow_legacy_reflection=allow_legacy_reflection,
            ).tolist()
    return normalized
