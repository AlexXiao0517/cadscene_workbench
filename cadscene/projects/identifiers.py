from __future__ import annotations

import re


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_project_id(value: str) -> str:
    """Return a safe, non-relative project directory key."""

    if not isinstance(value, str) or value in {".", ".."} or not _SAFE_ID.fullmatch(value):
        raise ValueError("invalid project_id")
    return value


def is_safe_stable_id(value: object) -> bool:
    return isinstance(value, str) and value not in {".", ".."} and bool(
        _SAFE_ID.fullmatch(value)
    )
