from __future__ import annotations

import sys
from typing import Sequence

from cadscene.cli import doctor, serve_viewer


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"serve", "doctor"}:
        print("usage: cadscene-workbench {serve|doctor} ...", file=sys.stderr)
        return 2
    command, *forwarded = arguments
    if command == "serve":
        return serve_viewer.main(forwarded)
    return doctor.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())
