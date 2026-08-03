from __future__ import annotations

import argparse
from pathlib import Path

from cadscene.video_analysis.clip_export import X264_PRESETS, export_video_clips


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export physical MP4 clips from a manifest.")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--preset", choices=X264_PRESETS, default="fast")
    parser.add_argument("--crf", type=int, default=18)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    exported = export_video_clips(
        video_path=args.video,
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        ffmpeg_executable=args.ffmpeg,
        preset=args.preset,
        crf=args.crf,
    )
    print(args.output_dir.resolve())
    print(f"Exported {len(exported)} clips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
