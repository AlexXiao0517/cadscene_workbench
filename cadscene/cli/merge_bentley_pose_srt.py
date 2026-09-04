"""Create a full-pose DJI SRT using camera orientations from Bentley XML."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from cadscene.srt.bentley_pose_merge import (
    load_bentley_pose_samples,
    merge_bentley_orientations_into_srt,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只将 Bentley XML 相机姿态合并到 DJI SRT，保留原始位置和高度。"
    )
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--xml", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--horizontal-fov-deg", type=float, default=59.109)
    return parser


def _write_temporary(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def _publish_pair_without_overwrite(
    output: Path,
    output_payload: bytes,
    report: Path,
    report_payload: bytes,
) -> None:
    if output.resolve(strict=False) == report.resolve(strict=False):
        raise ValueError("SRT output and report paths must be different")
    if output.exists() or report.exists():
        raise FileExistsError("refusing to overwrite merge output")
    temporary_output: Path | None = None
    temporary_report: Path | None = None
    published_output = False
    published_report = False
    try:
        temporary_output = _write_temporary(output, output_payload)
        temporary_report = _write_temporary(report, report_payload)
        os.link(temporary_output, output)
        published_output = True
        os.link(temporary_report, report)
        published_report = True
    except Exception:
        if published_report:
            report.unlink(missing_ok=True)
        if published_output:
            output.unlink(missing_ok=True)
        raise
    finally:
        if temporary_output is not None:
            temporary_output.unlink(missing_ok=True)
        if temporary_report is not None:
            temporary_report.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        for path, label in ((args.srt, "SRT"), (args.xml, "Bentley XML")):
            if not path.is_file():
                raise FileNotFoundError(f"{label} input is missing: {path}")
        source_paths = {
            args.srt.resolve(strict=True),
            args.xml.resolve(strict=True),
        }
        destinations = {
            args.output.resolve(strict=False),
            args.report.resolve(strict=False),
        }
        if source_paths & destinations:
            raise ValueError("merge destinations must not overwrite source inputs")

        source_bytes = args.srt.read_bytes()
        with args.srt.open("r", encoding="utf-8-sig", newline="") as stream:
            source_text = stream.read()
        samples = load_bentley_pose_samples(args.xml)
        result = merge_bentley_orientations_into_srt(
            source_text,
            samples,
            horizontal_fov_deg=args.horizontal_fov_deg,
        )
        output_payload = result.text.encode("utf-8")
        report_payload = {
            **result.report,
            "source_srt": str(args.srt.resolve(strict=True)),
            "source_xml": str(args.xml.resolve(strict=True)),
            "output_srt": str(args.output.resolve(strict=False)),
            "source_srt_sha256": sha256(source_bytes).hexdigest(),
            "source_xml_sha256": sha256(args.xml.read_bytes()).hexdigest(),
            "output_srt_sha256": sha256(output_payload).hexdigest(),
        }
        report_bytes = (
            json.dumps(report_payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        _publish_pair_without_overwrite(
            args.output,
            output_payload,
            args.report,
            report_bytes,
        )
        print(f"完整姿态 SRT: {args.output}")
        print(f"合并审计报告: {args.report}")
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
