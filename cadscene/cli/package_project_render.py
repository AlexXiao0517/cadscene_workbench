from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess

from cadscene.projects.media import ProjectMediaSpec
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize one legacy workbench render into a project render attempt."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-frame-map", required=True, type=Path)
    parser.add_argument("--media-spec", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        spec = ProjectMediaSpec.from_dict(
            json.loads(args.media_spec.read_text(encoding="utf-8"))
        )
        source_map = json.loads(args.source_frame_map.read_text(encoding="utf-8"))
        render_map = _render_frame_map(source_map)
        output_dir = args.output_dir.resolve(strict=True)
        temporary = output_dir / "rendered.tmp.mp4"
        output = output_dir / "rendered.mp4"
        command = _normalization_command(
            ffmpeg=str(resolve_ffmpeg_executable(args.ffmpeg)),
            source=args.input.resolve(strict=True),
            target=temporary,
            spec=spec,
        )
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return int(completed.returncode)
        os.replace(temporary, output)
        _atomic_json(output_dir / "render_frame_map.json", render_map)
        return 0
    except Exception as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1


def _render_frame_map(source_map: object) -> dict[str, object]:
    if not isinstance(source_map, dict):
        raise ValueError("source frame map must be an object")
    clips = source_map.get("clips")
    if not isinstance(clips, list) or len(clips) != 1:
        raise ValueError("source frame map must contain exactly one clip")
    frames = clips[0].get("frames") if isinstance(clips[0], dict) else None
    if not isinstance(frames, list) or not frames:
        raise ValueError("source frame map has no frames")
    entries = []
    for output_ordinal, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError("source frame entry must be an object")
        entries.append(
            {
                "output_frame_ordinal": output_ordinal,
                "source_decoded_frame_ordinal": int(frame["ordinal"]),
                "source_pts": int(frame["pts"]),
            }
        )
    return {
        "schema_version": 1,
        "source_time_base": source_map["source_time_base"],
        "frames": entries,
    }


def _normalization_command(
    *, ffmpeg: str, source: Path, target: Path, spec: ProjectMediaSpec
) -> tuple[str, ...]:
    sar = spec.sample_aspect_ratio
    time_base = spec.time_base
    if time_base.numerator != 1:
        raise ValueError("project MP4 time base numerator must be one")
    signal_range = "limited" if spec.color_range == "tv" else "full"
    filters = (
        f"scale={spec.width}:{spec.height}:flags=lanczos,"
        f"setsar={sar.numerator}/{sar.denominator},"
        f"setparams=range={signal_range}:color_primaries={spec.color_primaries}:"
        f"color_trc={spec.color_transfer}:colorspace={spec.color_space}"
    )
    return (
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-vf",
        filters,
        "-fps_mode",
        "passthrough",
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        spec.profile.casefold(),
        "-pix_fmt",
        spec.pixel_format,
        "-enc_time_base",
        f"{time_base.numerator}:{time_base.denominator}",
        "-video_track_timescale",
        str(time_base.denominator),
        "-color_range",
        spec.color_range,
        "-colorspace",
        spec.color_space,
        "-color_trc",
        spec.color_transfer,
        "-color_primaries",
        spec.color_primaries,
        "-movflags",
        "+faststart",
        str(target),
    )


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
