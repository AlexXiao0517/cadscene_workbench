from __future__ import annotations

import argparse
from pathlib import Path

from cadscene.projects.scene_bridge_runner import SceneBridgeInputs, run_scene_bridge


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build an exact-PTS route bridge between same-scene clips."
    )
    parser.add_argument("--inputs", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candidate = run_scene_bridge(SceneBridgeInputs.from_json(args.inputs))
    print(candidate.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
