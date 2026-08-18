from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from cadscene.pure_rotation.backend import ExternalOpenGVBackend
from cadscene.pure_rotation.raw_trajectory import convert_poc_raw_trajectory


def promote_output_tree(staged: Path, destination: Path, *, force: bool) -> None:
    if not destination.exists():
        os.replace(staged, destination)
        return
    if not force:
        raise FileExistsError(f"pure-rotation output already exists: {destination}")
    backup = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except BaseException:
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        os.replace(backup, destination)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--backend-root")
    parser.add_argument("--backend-command")
    parser.add_argument("--backend-command-json")
    parser.add_argument("--cadscene-readonly", type=Path)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.output_root / args.dataset / args.run_id / "02_pure_rotation"
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=".pure-rotation-run-", dir=str(output.parent)))
    staged_output = staging_root / output.name
    backend_command = None
    if args.backend_command_json:
        decoded = json.loads(args.backend_command_json)
        if (
            not isinstance(decoded, list)
            or not decoded
            or not all(isinstance(item, str) and item for item in decoded)
        ):
            parser.error("--backend-command-json must be a non-empty string array")
        backend_command = decoded
    elif args.backend_command:
        backend_command = [args.backend_command]
    backend = ExternalOpenGVBackend(
        backend_root=args.backend_root,
        backend_command=backend_command,
    )

    def report(stage: str, message: str, fraction: float) -> None:
        if args.progress_file is not None:
            _write_progress(args.progress_file, stage, message, fraction)

    try:
        report("pure_rotation", "正在运行纯旋转轨迹反算", 0.05)
        result = backend.run_video(
            video=args.video,
            cadscene_readonly=args.cadscene_readonly or args.output_root.parents[0],
            output_dir=staged_output,
        )
        report("converting", "轨迹和预览已生成，正在转换工作台轨迹", 0.90)
        trajectory = json.loads(
            (staged_output / "full_video_rotation_trajectory.json").read_text(encoding="utf-8")
        )
        pts: dict[int, float] = {}
        with (staged_output / "full_video_pairwise_rotations.csv").open(
            encoding="utf-8-sig",
            newline="",
        ) as stream:
            for row in csv.DictReader(stream):
                if row.get("decoded_frame_index_1"):
                    pts.setdefault(
                        int(row["decoded_frame_index_1"]),
                        float(row.get("timestamp_seconds_1") or row.get("pts_1") or 0),
                    )
                if row.get("decoded_frame_index_2"):
                    pts[int(row["decoded_frame_index_2"])] = float(
                        row.get("timestamp_seconds_2") or row.get("pts_2") or 0
                    )
        raw = convert_poc_raw_trajectory(trajectory, pts_by_decoded_index=pts)
        (staged_output / "camera_rotation_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (staged_output / "backend_summary.json").write_text(
            json.dumps(result["summary"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        report("publishing", "汇总文件已完成，正在发布结果", 0.96)
        promote_output_tree(staged_output, output, force=args.force)
        report("ready_for_validation", "结果已发布，正在执行最终校验", 0.98)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return 0


def _write_progress(
    path: Path, stage: str, message: str, fraction: float
) -> None:
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "message": message,
        "fraction": max(0.0, min(1.0, float(fraction))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.005 * (2**attempt), 0.05))
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
