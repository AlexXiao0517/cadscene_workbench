from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze source PTS and publish logical video clip recommendations."
    )
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--srt", type=Path)
    parser.add_argument("--analysis-revision")
    parser.add_argument("--sample-interval-sec", type=float, default=0.5)
    parser.add_argument("--ffmpeg", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from cadscene.video_analysis.analyzer import analyze_video

    published = analyze_video(
        video_path=args.video,
        output_root=args.output_root,
        project_id=args.project_id,
        srt_path=args.srt,
        analysis_revision=args.analysis_revision,
        sample_interval_sec=args.sample_interval_sec,
        ffmpeg_executable=args.ffmpeg,
    )
    print(published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

