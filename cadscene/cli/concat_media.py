from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from cadscene.projects.concat_executor import (
    execute_concat_media,
    execution_plan_from_payload,
)
from cadscene.projects.media import ProjectMediaSpec


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one immutable project concat snapshot")
    parser.add_argument("--execution-plan-json", type=Path, required=True)
    parser.add_argument("--project-media-spec-json", type=Path, required=True)
    parser.add_argument("--attempt-directory", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        execution_payload = json.loads(arguments.execution_plan_json.read_text(encoding="utf-8"))
        spec_payload = json.loads(arguments.project_media_spec_json.read_text(encoding="utf-8"))
        execution = execution_plan_from_payload(execution_payload)
        if arguments.attempt_directory.is_symlink():
            raise ValueError("attempt directory must not be a symlink")
        trusted_attempt = arguments.attempt_directory.resolve(strict=True)
        if not trusted_attempt.is_dir():
            raise ValueError("attempt directory must be an existing regular directory")
        if execution.attempt_directory.resolve(strict=True) != trusted_attempt:
            raise ValueError("execution snapshot attempt directory is not authorized")
        spec = ProjectMediaSpec.from_dict(spec_payload)
        result = execute_concat_media(
            execution, project_media_spec=spec, ffmpeg_executable=arguments.ffmpeg
        )
        payload = {
            "status": result.status,
            "output_revision": result.output_revision,
            "output_fingerprint": result.output_fingerprint,
            "outputs": dict(result.outputs),
            "error": result.error,
            "progress": [item.to_dict() for item in result.progress],
            "validation_proof": result.validation_proof,
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        payload = {"status": "failed", "error": str(exc)}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if payload["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
