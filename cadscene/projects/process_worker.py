from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--task-token", required=True)
    parser.add_argument("--command-fingerprint", required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("worker plan must contain commands")
    for command in commands:
        if not isinstance(command, list) or not command:
            raise ValueError("each worker command must be a non-empty list")
        completed = subprocess.run([str(item) for item in command], check=False)
        if completed.returncode != 0:
            return int(completed.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
