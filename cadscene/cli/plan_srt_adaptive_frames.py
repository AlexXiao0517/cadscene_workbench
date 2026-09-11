"""Plan adaptive COLMAP frames for the incomplete-SRT workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from cadscene.dji.metadata import load_dji_pose_priors
from cadscene.sfm.adaptive_sampling import select_adaptive_ordinals
from cadscene.srt.fixed_track_visual_pose import FixedTrackVisualPoseConfig, build_fixed_track_positions
from cadscene.srt.parser import load_srt_records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--srt", required=True, type=Path)
    parser.add_argument("--frame-map", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _sharpness(video: Path, frames: list[int]) -> np.ndarray:
    import cv2

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video}")
    values = []
    try:
        for frame in frames:
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(frame))
            ok, image = capture.read()
            if not ok or image is None:
                values.append(0.0)
                continue
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            scale = min(1.0, 480.0 / max(1.0, float(gray.shape[1])))
            if scale < 1.0:
                gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            values.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
    finally:
        capture.release()
    array = np.asarray(values, dtype=np.float64)
    if not np.any(array > 0.0):
        array[:] = 1.0
    return array


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    dji = load_dji_pose_priors(args.video, candidate_frames)
    rotations = (
        dji.world_from_camera
        if dji.available
        else np.repeat(np.eye(3)[None, :, :], len(candidate_frames), axis=0)
    )
    plan = select_adaptive_ordinals(centers, rotations, _sharpness(args.video, candidate_frames))
    source_frames = [candidate_frames[index] for index in plan.selected_ordinals]
    result = {
        "schema_version": "adaptive_sfm_frame_plan_v1",
        "candidate_spacing_sec": 0.5,
        "base_spacing_sec": 1.0,
        "candidate_count": len(candidate_frames),
        "selected_count": len(source_frames),
        "source_frames": source_frames,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
