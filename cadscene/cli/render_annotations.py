from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from cadscene.annotations.render_overlay import (
    build_annotation_events,
    build_overlay_concat_document,
    load_camera_rows,
    render_callout_overlay,
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
    *, ffmpeg: str, source: Path, target: Path, overlay_concat: Path
) -> tuple[str, ...]:
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(overlay_concat),
        "-filter_complex",
        "[0:v][1:v]overlay=0:0:format=auto:shortest=1[annotated]",
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
            frames = bundle.get("source_frames")
            time_base = bundle.get("source_time_base")
            if not isinstance(frames, list) or not isinstance(time_base, dict):
                raise ValueError("annotation render bundle requires authoritative frames")
            events_by_pts: dict[int, list[object]] = {}
            for event in events:
                events_by_pts.setdefault(event.source_pts, []).append(event)
            with tempfile.TemporaryDirectory(
                prefix=f".{output.stem}.callout-", dir=output.parent
            ) as directory:
                overlay_directory = Path(directory)
                overlay_paths: list[Path] = []
                for ordinal, frame in enumerate(frames):
                    source_pts = int(frame["source_pts"])
                    path = overlay_directory / f"overlay-{ordinal:08d}.png"
                    render_callout_overlay(
                        events_by_pts.get(source_pts, ()),
                        width=int(bundle["video_width"]),
                        height=int(bundle["video_height"]),
                    ).save(path, format="PNG", optimize=False)
                    overlay_paths.append(path)
                concat_path = overlay_directory / "overlay.ffconcat"
                concat_path.write_text(
                    build_overlay_concat_document(
                        overlay_paths,
                        source_frames=frames,
                        time_base_numerator=int(time_base["numerator"]),
                        time_base_denominator=int(time_base["denominator"]),
                    ),
                    encoding="utf-8",
                    newline="\n",
                )
                command = _ffmpeg_command(
                    ffmpeg=str(resolve_ffmpeg_executable(args.ffmpeg)),
                    source=source,
                    target=temporary,
                    overlay_concat=concat_path,
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


if __name__ == "__main__":
    raise SystemExit(main())
