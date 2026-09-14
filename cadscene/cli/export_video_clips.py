from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from cadscene.video_analysis.clip_export import X264_PRESETS, export_video_clips


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export physical MP4 clips from a manifest."
    )
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--preset", choices=X264_PRESETS, default="fast")
    parser.add_argument("--crf", type=int, default=18)
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument("--max-duration-seconds", type=int, default=60)
    duration.add_argument(
        "--no-duration-limit",
        action="store_true",
        help="allow an unsegmented source-length clip",
    )
    parser.add_argument(
        "--reuse-source-full-span",
        action="store_true",
        help="reuse a browser-compatible full-span MP4 instead of re-encoding it",
    )
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument(
        "--allow-subset",
        action="store_true",
        help="export only manifest intervals without claiming full-source partition",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = {
        "ffmpeg_executable": args.ffmpeg,
        "preset": args.preset,
        "crf": args.crf,
        "require_full_source_partition": not args.allow_subset,
        "max_duration_seconds": (
            None if args.no_duration_limit else args.max_duration_seconds
        ),
    }
    if args.reuse_source_full_span:
        options["reuse_source_if_full_span"] = True
    if args.progress_file is not None:
        options["progress_callback"] = lambda stage, message, fraction: (
            _write_progress(args.progress_file, stage, message, fraction)
        )
    exported = export_video_clips(
        video_path=args.video,
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        **options,
    )
    print(args.output_dir.resolve())
    print(f"Exported {len(exported)} clips")
    return 0


def _write_progress(
    path: Path, stage: str, message: str, fraction: float
) -> None:
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


if __name__ == "__main__":
    raise SystemExit(main())
