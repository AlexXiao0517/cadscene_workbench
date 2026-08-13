from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from cadscene.annotations.render_overlay import (
    build_annotation_events,
    build_drawtext_filter,
    build_sendcmd_document,
    load_camera_rows,
)
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Burn Stage 9 visual annotations into an existing clip render."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--camera-path", type=Path)
    parser.add_argument("--ffmpeg")
    return parser


def _ffmpeg_command(
    *, ffmpeg: str, source: Path, target: Path, filter_path: Path
) -> tuple[str, ...]:
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-filter_complex_script",
        str(filter_path),
        "-map",
        "[annotated]",
        "-fps_mode",
        "passthrough",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    temporary: Path | None = None
    resources: list[Path] = []
    try:
        source = args.input.resolve(strict=True)
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        if not isinstance(bundle, dict):
            raise ValueError("annotation render bundle must be an object")
        camera_rows = load_camera_rows(
            args.camera_path.resolve(strict=True)
            if args.camera_path is not None
            else None
        )
        events = build_annotation_events(bundle, camera_rows=camera_rows)
        output = args.output.resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.stem}.tmp{output.suffix}")
        if not events:
            shutil.copy2(source, temporary)
        else:
            command_text, targets = build_sendcmd_document(events)
            command_path = output.with_name(f".{output.stem}.annotations.cmd")
            command_path.write_text(command_text, encoding="utf-8", newline="\n")
            resources.append(command_path)
            text_paths: dict[str, Path] = {}
            for index, annotation_id in enumerate(sorted(targets)):
                event = next(
                    item for item in events if item.annotation_id == annotation_id
                )
                text_path = output.with_name(
                    f".{output.stem}.annotation-{index:03d}.txt"
                )
                text_path.write_text(event.text, encoding="utf-8", newline="\n")
                resources.append(text_path)
                text_paths[annotation_id] = text_path
            filter_path = output.with_name(f".{output.stem}.annotations.ffscript")
            filter_path.write_text(
                build_drawtext_filter(
                    events,
                    command_path=command_path,
                    text_paths=text_paths,
                    targets=targets,
                ),
                encoding="utf-8",
                newline="\n",
            )
            resources.append(filter_path)
            command = _ffmpeg_command(
                ffmpeg=str(resolve_ffmpeg_executable(args.ffmpeg)),
                source=source,
                target=temporary,
                filter_path=filter_path,
            )
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                return int(completed.returncode)
        os.replace(temporary, output)
        temporary = None
        print(output)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        for resource in resources:
            resource.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
