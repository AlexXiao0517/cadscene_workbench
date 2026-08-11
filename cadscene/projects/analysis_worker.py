from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from cadscene.video_analysis.analyzer import analyze_video
from cadscene.workflow.data_import import import_cad


class AttemptProgressReporter:
    """Atomically publishes adapter-owned, structured attempt progress."""

    def __init__(self, attempt_dir: Path) -> None:
        self.path = attempt_dir / "adapter_progress.json"

    def report(self, stage: str, message: str, fraction: float | None) -> None:
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "stage": stage,
            "message": message,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if fraction is not None:
            payload["fraction"] = max(0.0, min(1.0, float(fraction)))
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            for attempt in range(20):
                try:
                    os.replace(temporary, self.path)
                    return
                except PermissionError as exc:
                    if attempt == 19:
                        print(
                            f"warning: progress update skipped after Windows file-lock retries: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                        return
                    time.sleep(0.01)
        finally:
            temporary.unlink(missing_ok=True)


def _cad_progress(reporter: AttemptProgressReporter, message: str) -> None:
    if "design.json" in message:
        reporter.report("generating_cad", "正在生成 CAD 场景数据", 0.75)
    elif "DXF" in message:
        reporter.report("parsing_cad", "正在解析 DXF 图纸", 0.35)
    elif "DWG" in message:
        reporter.report("converting_cad", "正在转换 DWG 图纸", 0.25)
    elif "完成" in message:
        reporter.report("complete", "CAD 解析完成", 1.0)
    else:
        reporter.report("preparing_cad", "正在准备 CAD 数据", 0.10)


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
    reporter = AttemptProgressReporter(attempt)
    if args.phase == "cad":
        reporter.report("preparing_cad", "正在准备 CAD 解析", 0.0)
        with args.input.open("rb") as stream:
            import_cad(
                attempt / "scratch",
                args.project_id,
                args.original_filename,
                stream,
                status_callback=lambda message: _cad_progress(reporter, message),
            )
        reporter.report("complete", "CAD 解析完成", 1.0)
        return 0
    reporter.report("probing_pts", "正在准备视频分析", 0.0)
    analyze_video(
        video_path=args.input,
        output_root=attempt,
        project_id=args.project_id,
        srt_path=args.srt,
        analysis_revision=args.analysis_revision,
        progress_callback=reporter.report,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
