"""Plan adaptive COLMAP frames for the incomplete-SRT workflow."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

import numpy as np

from cadscene.dji.metadata import load_dji_pose_priors
from cadscene.sfm.adaptive_sampling import select_adaptive_ordinals
from cadscene.sfm.adaptive_frame_preparation import (
    hydrate_prepared_cache,
    preparation_identity,
    prepare_adaptive_candidates,
    publish_prepared_cache,
    publish_selected_candidates,
    validate_prepared_images,
)
from cadscene.srt.fixed_track_visual_pose import FixedTrackVisualPoseConfig, build_fixed_track_positions
from cadscene.srt.parser import load_srt_records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--frame-map", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--images-output", required=True, type=Path)
    parser.add_argument("--reconstruct-width", required=True, type=int)
    parser.add_argument("--reconstruct-height", required=True, type=int)
    parser.add_argument("--progress-file", type=Path)
    parser.add_argument("--cache-root", type=Path)
    return parser


def _write_progress(path: Path | None, stage: str, message: str, fraction: float) -> None:
    if path is None:
        return
    payload = {
        "schema_version": "1.0",
        "stage": stage,
        "message": message,
        "fraction": max(0.0, min(1.0, float(fraction))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reuse_prepared_plan(
    plan_path: Path,
    images_dir: Path,
    identity: dict[str, object],
    *,
    output_size: tuple[int, int],
) -> dict[str, object] | None:
    if not plan_path.is_file():
        return None
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return None
        source_frames = payload.get("source_frames")
        if (
            payload.get("preparation_identity") != identity
            or not isinstance(source_frames, list)
            or Path(str(payload.get("prepared_images_dir", ""))).resolve()
            != images_dir.resolve()
        ):
            return None
        validate_prepared_images(
            images_dir,
            source_frames=[int(frame) for frame in source_frames],
            expected_identity=identity,
            expected_size=output_size,
        )
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _write_progress(args.progress_file, "preparing_frames", "正在建立 SRT 求解帧计划", 0.01)
    payload = json.loads(args.config.read_text(encoding="utf-8-sig"))
    config = FixedTrackVisualPoseConfig.from_dict(payload["build"])
    metadata = payload["video_metadata"]
    fps = float(metadata["fps"])
    frame_map = json.loads(args.frame_map.read_text(encoding="utf-8-sig"))
    positions = build_fixed_track_positions(load_srt_records(args.srt), frame_map, config)
    by_frame = {item.frame_index: item for item in positions}
    last_frame = max(by_frame)
    candidate_step = max(1, int(round(fps * 0.5)))
    candidate_frames = list(range(0, last_frame + 1, candidate_step))
    if candidate_frames[-1] != last_frame:
        candidate_frames.append(last_frame)
    candidate_frames = [frame for frame in candidate_frames if frame in by_frame]
    centers = np.asarray([by_frame[frame].center for frame in candidate_frames], dtype=np.float64)
    output_size = (args.reconstruct_width, args.reconstruct_height)
    identity = preparation_identity(
        video=args.video,
        srt=args.srt,
        frame_map=args.frame_map,
        output_size=output_size,
        planner_settings={"candidate_spacing_sec": 0.5, "base_spacing_sec": 1.0},
    )
    if reuse_prepared_plan(
        args.output,
        args.images_output,
        identity,
        output_size=output_size,
    ) is not None:
        _write_progress(args.progress_file, "prepared_cache", "已复用验证通过的求解帧", 0.1)
        print("adaptive solve-image cache hit", flush=True)
        return 0
    if args.cache_root is not None and hydrate_prepared_cache(
        args.cache_root,
        args.output,
        args.images_output,
        identity=identity,
        output_size=output_size,
    ) is not None:
        _write_progress(
            args.progress_file,
            "prepared_cache",
            "已复用项目中验证通过的求解帧",
            0.1,
        )
        print("project adaptive solve-image cache hit", flush=True)
        return 0
    dji = load_dji_pose_priors(args.video, candidate_frames)
    rotations = (
        dji.world_from_camera
        if dji.available
        else np.repeat(np.eye(3)[None, :, :], len(candidate_frames), axis=0)
    )
    candidate_dir = args.images_output.parent / ".adaptive_candidates"
    last_reported = [-1]

    def report_decode(completed: int, total: int) -> None:
        if completed != total and completed - last_reported[0] < 5:
            return
        last_reported[0] = completed
        _write_progress(
            args.progress_file,
            "preparing_frames",
            f"正在顺序解码并准备求解帧 {completed}/{total}",
            0.02 + 0.06 * completed / max(1, total),
        )

    prepared = prepare_adaptive_candidates(
        args.video,
        candidate_frames,
        candidate_dir,
        output_size=output_size,
        progress=report_decode,
    )
    plan = select_adaptive_ordinals(centers, rotations, prepared.sharpness)
    source_frames = [candidate_frames[index] for index in plan.selected_ordinals]
    manifest = publish_selected_candidates(
        prepared,
        selected_frames=source_frames,
        images_dir=args.images_output,
        identity=identity,
    )
    result = {
        "schema_version": "adaptive_sfm_frame_plan_v1",
        "candidate_spacing_sec": 0.5,
        "base_spacing_sec": 1.0,
        "candidate_count": len(candidate_frames),
        "selected_count": len(source_frames),
        "source_frames": source_frames,
        "prepared_images_dir": str(args.images_output),
        "prepared_images_manifest": str(manifest),
        "preparation_identity": identity,
        "reasons": {
            str(candidate_frames[index]): list(plan.reasons[index])
            for index in plan.selected_ordinals
        },
        "dji_prior_available": dji.available,
        "dji_prior_reason": dji.reason,
        "dji_packet_count": dji.packet_count,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.cache_root is not None:
        publish_prepared_cache(
            args.output,
            args.images_output,
            args.cache_root,
            identity=identity,
            output_size=output_size,
        )
    _write_progress(
        args.progress_file,
        "prepared_frames",
        f"已准备 {len(source_frames)} 张求解帧，正在启动 COLMAP",
        0.1,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
