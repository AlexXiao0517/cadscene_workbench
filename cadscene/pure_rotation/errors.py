from __future__ import annotations


class PureRotationError(RuntimeError):
    error_code = "pure-rotation-error"

    def __str__(self) -> str:
        return f"{self.error_code}: {super().__str__()}"


class PureRotationBackendUnavailable(PureRotationError):
    error_code = "pure-rotation-backend-unavailable"


class PureRotationBackendFailed(PureRotationError):
    error_code = "pure-rotation-backend-failed"

