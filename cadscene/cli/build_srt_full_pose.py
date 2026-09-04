"""Build a complete SRT camera trajectory directly in local CAD metres."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from cadscene.srt.full_pose import (
    FullPoseBuildConfig,
    build_full_pose_trajectory,
)
from cadscene.srt.parser import load_srt_records
from cadscene.srt.full_pose_workbench import (
    build_full_pose_workbench_payloads,
    publish_full_pose_workbench_payloads,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 DJI SRT 全姿态和已确认的 CGCS2000 配置直接生成 CAD 相机轨迹。"
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--frame-map", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--progress-file", type=Path)
    return parser


def _write_progress(path: Path | None, stage: str, fraction: float) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "stage": stage,
                "message": stage,
                "fraction": fraction,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_root = args.output_root / args.dataset / args.run_id
    final_output = run_root / "02_srt_full_pose"
    staging_root: Path | None = None
    try:
        for path, label in (
            (args.video, "video"),
            (args.srt, "SRT"),
            (args.frame_map, "frame map"),
            (args.config, "configuration"),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"physical {label} input is missing: {path}")
        payload = json.loads(args.config.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("full-pose configuration must be an object")
        video_metadata = payload.get("video_metadata")
        build_payload = payload.get("build")
        if not isinstance(video_metadata, dict) or not isinstance(
            build_payload, dict
        ):
            raise ValueError(
                "full-pose configuration requires video_metadata and build objects"
            )
        config = FullPoseBuildConfig.from_dict(build_payload)
        if config.clip_id != args.run_id:
            raise ValueError("configuration clip_id must match --run-id")
        frame_map = json.loads(args.frame_map.read_text(encoding="utf-8-sig"))
        records = load_srt_records(args.srt)
        run_root.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(prefix=".srt-full-pose-", dir=run_root)
        )
        staged_output = staging_root / final_output.name
        _write_progress(args.progress_file, "building_full_pose", 0.1)
        build_full_pose_trajectory(
            records,
            frame_map,
            video_metadata,
            config,
            staged_output,
        )
        trajectory = json.loads(
            (staged_output / "camera_trajectory_full_pose.json").read_text(
                encoding="utf-8-sig"
            )
        )
        workbench_payloads = build_full_pose_workbench_payloads(trajectory)
        publish_full_pose_workbench_payloads(staging_root, workbench_payloads)
        _write_progress(args.progress_file, "publishing_full_pose", 0.95)
        target_names = ("02_srt_full_pose", "03_alignment", "05_viewer_scene")
        occupied = [name for name in target_names if (run_root / name).exists()]
        if occupied:
            raise FileExistsError(
                "full-pose output already exists: " + ", ".join(occupied)
            )
        for name in target_names:
            os.replace(staging_root / name, run_root / name)
        _write_progress(args.progress_file, "ready_for_validation", 0.98)
        print(f"输出路径: {final_output}")
        return 0
    except Exception as exc:
        print(str(exc), file=os.sys.stderr)
        return 1
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
