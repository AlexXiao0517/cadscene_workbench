from __future__ import annotations

import re


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_WINDOWS_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def validate_project_id(value: str) -> str:
    """Return a safe, non-relative project directory key."""

    device_stem = value.split(".", 1)[0].upper() if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or value.endswith((".", " "))
        or device_stem in _WINDOWS_DEVICES
        or not _SAFE_ID.fullmatch(value)
    ):
        raise ValueError("invalid project_id")
    return value


def is_safe_stable_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_project_id(value)
    except ValueError:
        return False
    return True
