from __future__ import annotations

import argparse
from hashlib import sha256
import hmac
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
    normalized: list[list[str]] = []
    for command in commands:
        if not isinstance(command, list) or not command:
            raise ValueError("each worker command must be a non-empty list")
        normalized.append([str(item) for item in command])
    canonical = json.dumps(
        normalized, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    actual_fingerprint = sha256(canonical).hexdigest()
    if not hmac.compare_digest(actual_fingerprint, args.command_fingerprint):
        raise ValueError("worker plan command fingerprint mismatch")
    for command in normalized:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return int(completed.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
