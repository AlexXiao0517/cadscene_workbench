from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path
from typing import Mapping, Sequence

from cadscene.projects.source_fallback import (
    render_source_interval,
    source_interval_inputs_from_payload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render one decoded-frame integer-PTS source interval."
    )
    parser.add_argument("--project-id", default="standalone")
    parser.add_argument("--source-video", type=Path, required=True)
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--source-start-pts", type=int, required=True)
    parser.add_argument("--source-end-pts-exclusive", type=int, required=True)
    parser.add_argument("--source-time-base", type=_positive_fraction, required=True)
    media = parser.add_mutually_exclusive_group(required=True)
    media.add_argument("--project-media-spec", type=Path)
    media.add_argument("--project-media-spec-json")
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument("--preset", default="fast")
    parser.add_argument("--crf", type=int, default=18)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    media_spec = _load_media_spec(args)
    inputs = source_interval_inputs_from_payload(
        project_id=args.project_id,
        clip_id=args.clip_id,
        source_video_path=args.source_video.resolve(),
        source_start_pts=args.source_start_pts,
        source_end_pts_exclusive=args.source_end_pts_exclusive,
        source_time_base=args.source_time_base,
        attempt_directory=args.attempt_dir.resolve(),
        project_media_spec=media_spec,
    )
    render_source_interval(
        inputs,
        ffmpeg_executable=args.ffmpeg,
        ffprobe_executable=args.ffprobe,
        preset=args.preset,
        crf=args.crf,
    )
    return 0


def _load_media_spec(args: argparse.Namespace) -> Mapping[str, object]:
    if args.project_media_spec is not None:
        payload = json.loads(args.project_media_spec.read_text(encoding="utf-8"))
    else:
        payload = json.loads(args.project_media_spec_json)
    if not isinstance(payload, Mapping):
        raise ValueError("project media specification must be a JSON object")
    return payload


def _positive_fraction(value: str) -> Fraction:
    parts = value.split("/")
    if (
        len(parts) != 2
        or not parts[0].lstrip("+-").isdigit()
        or not parts[1].lstrip("+-").isdigit()
    ):
        raise argparse.ArgumentTypeError(
            "expected a positive rational numerator/denominator"
        )
    numerator = int(parts[0])
    denominator = int(parts[1])
    if numerator <= 0 or denominator <= 0:
        raise argparse.ArgumentTypeError(
            "expected a positive rational numerator/denominator"
        )
    return Fraction(numerator, denominator)


if __name__ == "__main__":
    raise SystemExit(main())
