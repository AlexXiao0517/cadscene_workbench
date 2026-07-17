from __future__ import annotations

import argparse
import json

from cadscene.sfm.backend_detection import detect_sfm_environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查 pycolmap、官方 COLMAP CLI 与 CUDA 可用性。")
    parser.add_argument("--colmap-exe")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = detect_sfm_environment(args.colmap_exe, requested_device=args.device)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

