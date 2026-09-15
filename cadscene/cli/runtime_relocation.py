from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence
import uuid


_OPENCV_CONFIG = """import os

_CV2_PACKAGE = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_ROOT = os.path.abspath(os.path.join(_CV2_PACKAGE, "..", "..", ".."))
BINARIES_PATHS = [os.path.join(_RUNTIME_ROOT, "Library", "bin")] + BINARIES_PATHS
"""

_OPENCV_CONFIG_3 = """import os

_CV2_PACKAGE = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXTENSIONS_PATHS = [
    os.path.join(_CV2_PACKAGE, "python-3")
] + PYTHON_EXTENSIONS_PATHS
"""


def repair_opencv_runtime_paths(runtime: Path) -> tuple[Path, ...]:
    """将 OpenCV 的一次性构建路径替换为随整合包移动的相对路径。"""

    runtime = Path(os.path.abspath(runtime))
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError(f"runtime directory is unavailable: {runtime}")
    cv2_root = runtime / "Lib" / "site-packages" / "cv2"
    configs = (
        (cv2_root / "config.py", _OPENCV_CONFIG),
        (cv2_root / "config-3.py", _OPENCV_CONFIG_3),
    )
    repaired: list[Path] = []
    for path, content in configs:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"OpenCV runtime config is unavailable: {path}")
        if path.read_text(encoding="utf-8-sig") == content:
            continue
        temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        repaired.append(path)
    return tuple(repaired)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="修复可移动整合包的运行时路径")
    parser.add_argument("--runtime", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repaired = repair_opencv_runtime_paths(arguments.runtime)
    print(json.dumps({"repaired": [str(path) for path in repaired]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
