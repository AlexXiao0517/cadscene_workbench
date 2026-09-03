from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Mapping
from uuid import uuid4

from cadscene.cli._progress import write_progress_sidecar
from cadscene.srt.georeference import (
    parse_central_meridian,
    recommend_cgcs2000_candidates,
)
from cadscene.srt.parser import load_srt_records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one immutable CAD georeference candidate set"
    )
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--progress-file", required=True, type=Path)
    return parser


def _load_request(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError("candidate request root must be an object")
    if int(value.get("schema_version", 0)) != 1:
        raise ValueError("candidate request schema_version must be 1")
    if str(value.get("algorithm_version") or "") not in {"1", "2"}:
        raise ValueError("candidate algorithm_version must be 1 or 2")
    if not str(value.get("input_fingerprint") or ""):
        raise ValueError("candidate input_fingerprint must not be empty")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        request = _load_request(args.request)
        srt_path = Path(str(request.get("srt_path") or ""))
        if not srt_path.is_file():
            raise FileNotFoundError("candidate request SRT is unavailable")
        records = load_srt_records(srt_path)
        gps = tuple(
            (float(record.longitude), float(record.latitude))
            for record in records
            if record.longitude is not None and record.latitude is not None
        )
        if not gps:
            raise ValueError("SRT contains no usable WGS84 longitude/latitude samples")
        central_meridian = parse_central_meridian(
            request.get("central_meridian_deg")
        )
        candidates = recommend_cgcs2000_candidates(
            [item[0] for item in gps],
            [item[1] for item in gps],
            request["cad_bbox_raw"],  # type: ignore[arg-type]
            limit=int(request.get("limit", 6)),
            central_meridian_deg=central_meridian,
            progress_callback=lambda stage, message, fraction: write_progress_sidecar(
                args.progress_file, stage, message, fraction
            ),
        )
        _atomic_write_json(
            args.output,
            {
                "schema_version": 1,
                "algorithm_version": str(request["algorithm_version"]),
                "input_fingerprint": str(request["input_fingerprint"]),
                "central_meridian_deg": central_meridian,
                "limit": int(request.get("limit", 6)),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [item.to_dict() for item in candidates],
            },
        )
        return 0
    except Exception as exc:
        write_progress_sidecar(
            args.progress_file,
            "failed",
            f"坐标系候选生成失败：{exc}",
            None,
        )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
