from __future__ import annotations

import argparse
from pathlib import Path

from cadscene.video_analysis.analyzer import analyze_video
from cadscene.workflow.data_import import import_cad


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one immutable project-analysis attempt"
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)
    cad = subparsers.add_parser("cad")
    cad.add_argument("--project-id", required=True)
    cad.add_argument("--input", type=Path, required=True)
    cad.add_argument("--original-filename", required=True)
    cad.add_argument("--attempt-dir", type=Path, required=True)
    video = subparsers.add_parser("video")
    video.add_argument("--project-id", required=True)
    video.add_argument("--input", type=Path, required=True)
    video.add_argument("--srt", type=Path)
    video.add_argument("--analysis-revision", required=True)
    video.add_argument("--attempt-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    attempt = args.attempt_dir.resolve()
    attempt.mkdir(parents=True, exist_ok=True)
    if args.phase == "cad":
        with args.input.open("rb") as stream:
            import_cad(
                attempt / "scratch",
                args.project_id,
                args.original_filename,
                stream,
            )
        return 0
    analyze_video(
        video_path=args.input,
        output_root=attempt,
        project_id=args.project_id,
        srt_path=args.srt,
        analysis_revision=args.analysis_revision,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
