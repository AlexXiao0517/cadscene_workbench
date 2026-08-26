from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping


REQUIRED_ARTIFACTS = (
    "video_analysis_manifest.json",
    "video_metadata.json",
    "analysis_windows.csv",
    "detected_boundaries.json",
    "clip_manifest.json",
    "video_analysis_report.md",
)
CURRENT_REVISION_POINTER = "current_analysis_revision.json"


def _validate_payloads(revision: str, payloads: Mapping[str, str]) -> None:
    missing = set(REQUIRED_ARTIFACTS) - set(payloads)
    extra = set(payloads) - set(REQUIRED_ARTIFACTS)
    if missing:
        raise ValueError(f"missing required artifacts: {sorted(missing)}")
    if extra:
        raise ValueError(f"unexpected artifacts: {sorted(extra)}")
    manifest = json.loads(payloads["video_analysis_manifest.json"])
    clips = json.loads(payloads["clip_manifest.json"])
    if manifest.get("analysis_revision") != revision:
        raise ValueError("manifest analysis_revision mismatch")
    if clips.get("analysis_revision") != revision:
        raise ValueError("clip manifest analysis_revision mismatch")
    for clip in clips.get("clips", []):
        duration = float(clip["source_end_pts_sec"]) - float(clip["source_start_pts_sec"])
        if duration > 60.0 + 1e-9:
            raise ValueError(f"clip {clip.get('clip_id', '')} exceeds 60 second hard limit")
        if duration <= 0:
            raise ValueError(f"clip {clip.get('clip_id', '')} has non-positive duration")


def publish_analysis_revision(
    output_dir: Path, revision: str, payloads: Mapping[str, str]
) -> Path:
    _validate_payloads(revision, payloads)
    revisions_dir = output_dir / "analysis_revisions"
    destination = revisions_dir / revision
    if destination.exists():
        raise FileExistsError(f"analysis revision already exists: {revision}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".va-", dir=output_dir.parent))
    staged_revision = staging_root / "r"
    previous_files = {
        name: (output_dir / name).read_bytes() if (output_dir / name).is_file() else None
        for name in REQUIRED_ARTIFACTS
    }
    pointer_path = output_dir / CURRENT_REVISION_POINTER
    previous_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
    try:
        staged_revision.mkdir()
        for name, content in payloads.items():
            (staged_revision / name).write_text(content, encoding="utf-8", newline="")
        _validate_payloads(
            revision,
            {name: (staged_revision / name).read_text(encoding="utf-8") for name in REQUIRED_ARTIFACTS},
        )

        revisions_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staged_revision, destination)
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_ARTIFACTS:
            staged_root_file = staging_root / name
            shutil.copyfile(destination / name, staged_root_file)
            os.replace(staged_root_file, output_dir / name)
        staged_pointer = staging_root / CURRENT_REVISION_POINTER
        staged_pointer.write_text(
            json.dumps(
                {
                    "analysis_revision": revision,
                    "revision_directory": f"analysis_revisions/{revision}",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(staged_pointer, pointer_path)
        return destination
    except Exception:
        for name, previous in previous_files.items():
            root_file = output_dir / name
            if previous is None:
                root_file.unlink(missing_ok=True)
            else:
                restore = staging_root / f"restore-{name}"
                restore.write_bytes(previous)
                os.replace(restore, root_file)
        if previous_pointer is None:
            pointer_path.unlink(missing_ok=True)
        else:
            restore_pointer = staging_root / f"restore-{CURRENT_REVISION_POINTER}"
            restore_pointer.write_bytes(previous_pointer)
            os.replace(restore_pointer, pointer_path)
        if destination.exists():
            shutil.rmtree(destination)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
