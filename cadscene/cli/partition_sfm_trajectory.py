from __future__ import annotations

import argparse
from pathlib import Path

from cadscene.sfm.trajectory_partition import partition_sfm_trajectory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind one SfM solve trajectory to exact source PTS and core frames."
    )
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--solve-frame-map", required=True, type=Path)
    parser.add_argument("--core-frame-map", required=True, type=Path)
    parser.add_argument("--solve-output", required=True, type=Path)
    parser.add_argument("--core-output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = partition_sfm_trajectory(
        args.trajectory,
        args.solve_frame_map,
        args.core_frame_map,
        solve_output_path=args.solve_output,
        core_output_path=args.core_output,
    )
    print(result.core_path.resolve())
    print(
        f"Partitioned {result.solve_pose_count} solve poses into "
        f"{result.core_pose_count} core poses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
