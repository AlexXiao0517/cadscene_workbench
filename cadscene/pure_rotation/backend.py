from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Sequence

from .errors import PureRotationBackendFailed, PureRotationBackendUnavailable


POC_COMMIT = "85ab6404bfb5a07da8cdaaba0a1e5c4da10dc250"
OPENGV_COMMIT = "91f4b19c73450833a40e463ad3648aae80b3a7f3"
REQUIRED_OUTPUTS = (
    "summary.json",
    "full_video_rotation_trajectory.json",
    "full_video_pairwise_rotations.csv",
    "pure_rotation_intervals.json",
)


class ExternalOpenGVBackend:
    """Subprocess-only adapter for the separately versioned POC repository."""

    def __init__(
        self,
        *,
        backend_root: str | Path | None = None,
        backend_command: Sequence[str] | None = None,
    ) -> None:
        configured_root = backend_root or os.environ.get("PURE_ROTATION_BACKEND_ROOT")
        self.backend_root = Path(configured_root).expanduser().resolve() if configured_root else None
        self.backend_command = list(backend_command) if backend_command else None

    def _metadata(self) -> dict[str, Any]:
        if self.backend_root is None or not self.backend_root.is_dir():
            raise PureRotationBackendUnavailable("backend root is not configured or does not exist")
        version = self.backend_root / "backend_version.json"
        if version.is_file():
            try:
                return json.loads(version.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PureRotationBackendUnavailable("backend version metadata is unreadable") from exc
        return {
            "poc_commit": self._git_commit(self.backend_root),
            "opengv_commit": self._git_commit(self.backend_root / "third_party" / "opengv"),
        }

    @staticmethod
    def _git_commit(path: Path) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PureRotationBackendUnavailable("unable to audit backend Git commit") from exc
        if completed.returncode != 0:
            raise PureRotationBackendUnavailable("unable to audit backend Git commit")
        return completed.stdout.strip()

    def inspect_backend(self) -> dict[str, Any]:
        metadata = self._metadata()
        cli = self.backend_root / "scripts" / "run_full_video_exploration.py"  # type: ignore[operator]
        if not cli.is_file():
            raise PureRotationBackendUnavailable("required POC CLI is missing")
        if metadata.get("poc_commit") not in {None, POC_COMMIT}:
            raise PureRotationBackendUnavailable("POC commit does not match the approved pin")
        if metadata.get("opengv_commit") not in {None, OPENGV_COMMIT}:
            raise PureRotationBackendUnavailable("OpenGV commit does not match the approved pin")
        return {
            "available": True,
            "backend_root": str(self.backend_root),
            "cli": str(cli),
            "poc_commit": metadata.get("poc_commit") or POC_COMMIT,
            "opengv_commit": metadata.get("opengv_commit") or OPENGV_COMMIT,
        }

    def health_check(self) -> dict[str, Any]:
        try:
            return self.inspect_backend()
        except PureRotationBackendUnavailable as exc:
            return {"available": False, "error_code": exc.error_code, "message": str(exc)}

    def _command(self) -> list[str]:
        if self.backend_command:
            return list(self.backend_command)
        configured = os.environ.get("PURE_ROTATION_BACKEND_COMMAND")
        if configured:
            return [configured]
        raise PureRotationBackendUnavailable("backend command is not configured")

    def run_video(
        self,
        *,
        video: str | Path,
        cadscene_readonly: str | Path,
        output_dir: str | Path,
        timeout_seconds: float = 3600,
    ) -> dict[str, Any]:
        inspected = self.inspect_backend()
        video_path = Path(video)
        if not video_path.is_file():
            raise PureRotationBackendFailed("input video is unreadable")
        destination = Path(output_dir)
        if destination.exists():
            raise PureRotationBackendFailed("refusing to overwrite pure-rotation output")
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=str(destination.parent)))
        command = [
            *self._command(),
            "--video", str(video_path),
            "--cadscene-readonly", str(Path(cadscene_readonly)),
            "--output", str(temporary),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=timeout_seconds, check=False)
            (temporary / "backend_stdout.log").write_text(completed.stdout, encoding="utf-8")
            (temporary / "backend_stderr.log").write_text(completed.stderr, encoding="utf-8")
            if completed.returncode != 0:
                raise PureRotationBackendFailed(f"external backend exited with {completed.returncode}")
            missing = [name for name in REQUIRED_OUTPUTS if not (temporary / name).is_file()]
            if missing:
                raise PureRotationBackendFailed(f"backend output incomplete: {', '.join(missing)}")
            summary = json.loads((temporary / "summary.json").read_text(encoding="utf-8"))
            os.replace(temporary, destination)
            return {"backend": inspected, "summary": summary, "output_dir": str(destination), "command": command[:1]}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise PureRotationBackendFailed(str(exc)) from exc
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
