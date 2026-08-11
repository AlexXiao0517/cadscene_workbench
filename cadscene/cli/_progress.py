from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import uuid4


def write_progress_sidecar(
    path: Path, stage: str, message: str, fraction: float
) -> None:
    """Atomically publish adapter progress for the project executor."""
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "message": message,
        "fraction": max(0.0, min(1.0, float(fraction))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.005 * (2**attempt), 0.05))
    finally:
        temporary.unlink(missing_ok=True)
