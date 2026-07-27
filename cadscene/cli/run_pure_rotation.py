from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from cadscene.pure_rotation.backend import ExternalOpenGVBackend
from cadscene.pure_rotation.raw_trajectory import convert_poc_raw_trajectory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--backend-root")
    parser.add_argument("--backend-command")
    args = parser.parse_args()
    output = args.output_root / args.dataset / args.run_id / "02_pure_rotation"
    backend = ExternalOpenGVBackend(backend_root=args.backend_root, backend_command=[args.backend_command] if args.backend_command else None)
    result = backend.run_video(video=args.video, cadscene_readonly=args.output_root.parents[0], output_dir=output)
    trajectory = json.loads((output / "full_video_rotation_trajectory.json").read_text(encoding="utf-8"))
    pts: dict[int, float] = {}
    with (output / "full_video_pairwise_rotations.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("decoded_frame_index_1"):
                pts.setdefault(int(row["decoded_frame_index_1"]), float(row.get("timestamp_seconds_1") or row.get("pts_1") or 0))
            if row.get("decoded_frame_index_2"):
                pts[int(row["decoded_frame_index_2"])] = float(row.get("timestamp_seconds_2") or row.get("pts_2") or 0)
    raw = convert_poc_raw_trajectory(trajectory, pts_by_decoded_index=pts)
    (output / "camera_rotation_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "backend_summary.json").write_text(json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
