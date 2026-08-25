from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Sequence

from .errors import PureRotationBackendFailed, PureRotationBackendUnavailable


POC_COMMIT = "85ab6404bfb5a07da8cdaaba0a1e5c4da10dc250"
OPENGV_COMMIT = "91f4b19c73450833a40e463ad3648aae80b3a7f3"
REQUIRED_OUTPUTS = (
    "summary.json",
    "full_video_rotation_trajectory.json",
    "full_video_pairwise_rotations.csv",
    "pure_rotation_intervals.json",
)
_FRAME_PROGRESS = re.compile(r"evaluated through decoded frame\s+(\d+)")


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
                ["git", "-c", f"safe.directory={path.as_posix()}", "-C", str(path), "rev-parse", "HEAD"],
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
            if configured.lstrip().startswith("["):
                try:
                    parsed = json.loads(configured)
                except json.JSONDecodeError as exc:
                    raise PureRotationBackendUnavailable(
                        "backend command JSON is invalid"
                    ) from exc
                if (
                    not isinstance(parsed, list)
                    or not parsed
                    or not all(isinstance(item, str) and item for item in parsed)
                ):
                    raise PureRotationBackendUnavailable(
                        "backend command JSON must be a non-empty string array"
                    )
                return parsed
            return [configured]
        raise PureRotationBackendUnavailable("backend command is not configured")

    def run_video(
        self,
        *,
        video: str | Path,
        cadscene_readonly: str | Path,
        output_dir: str | Path,
        timeout_seconds: float = 3600,
        expected_frame_count: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
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
            if expected_frame_count is not None and expected_frame_count <= 0:
                raise PureRotationBackendFailed("expected frame count must be positive")
            completed = self._run_streaming(
                command,
                timeout_seconds=timeout_seconds,
                expected_frame_count=expected_frame_count,
                progress_callback=progress_callback,
            )
            (temporary / "backend_stdout.log").write_text(
                completed[1], encoding="utf-8"
            )
            (temporary / "backend_stderr.log").write_text(
                completed[2], encoding="utf-8"
            )
            if completed[0] != 0:
                diagnostic = completed[2].strip() or completed[1].strip()
                detail = diagnostic[-1000:] if diagnostic else ""
                suffix = f": {detail}" if detail else ""
                raise PureRotationBackendFailed(
                    f"external backend exited with {completed[0]}{suffix}"
                )
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

    @staticmethod
    def _run_streaming(
        command: Sequence[str],
        *,
        timeout_seconds: float,
        expected_frame_count: int | None,
        progress_callback: Callable[[int, int], None] | None,
    ) -> tuple[int, str, str]:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        streams = {"stdout": process.stdout, "stderr": process.stderr}

        def drain(name: str) -> None:
            stream = streams[name]
            if stream is None:
                events.put((name, None))
                return
            try:
                for line in stream:
                    events.put((name, line))
            finally:
                stream.close()
                events.put((name, None))

        readers = tuple(
            threading.Thread(
                target=drain,
                args=(name,),
                name=f"pure-rotation-{name}-reader",
                daemon=True,
            )
            for name in streams
        )
        for reader in readers:
            reader.start()
        output = {"stdout": [], "stderr": []}
        open_streams = set(streams)
        deadline = time.monotonic() + timeout_seconds
        try:
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(list(command), timeout_seconds)
                try:
                    name, line = events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    open_streams.discard(name)
                    continue
                output[name].append(line)
                if (
                    name == "stdout"
                    and expected_frame_count is not None
                    and progress_callback is not None
                    and (match := _FRAME_PROGRESS.search(line)) is not None
                ):
                    processed = min(int(match.group(1)) + 1, expected_frame_count)
                    progress_callback(processed, expected_frame_count)
            remaining = max(0.0, deadline - time.monotonic())
            returncode = process.wait(timeout=remaining)
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            for reader in readers:
                reader.join(1)
        return returncode, "".join(output["stdout"]), "".join(output["stderr"])
