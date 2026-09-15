from __future__ import annotations

import importlib
import os
from pathlib import Path


def test_opencv_runtime_paths_are_repaired_after_bundle_moves(tmp_path: Path) -> None:
    runtime = tmp_path / "moved-bundle" / "runtime"
    cv2_root = runtime / "Lib" / "site-packages" / "cv2"
    cv2_root.mkdir(parents=True)
    stale_root = "D:/build-machine/CADScene-0.1.3/runtime"
    (cv2_root / "config.py").write_text(
        "import os\n"
        f"BINARIES_PATHS = [os.path.join('{stale_root}/Library', 'bin')] "
        "+ BINARIES_PATHS\n",
        encoding="utf-8",
    )
    (cv2_root / "config-3.py").write_text(
        f"PYTHON_EXTENSIONS_PATHS = ['{stale_root}/Lib/site-packages/cv2/python-3'] "
        "+ PYTHON_EXTENSIONS_PATHS\n",
        encoding="utf-8",
    )

    module = importlib.import_module("cadscene.cli.runtime_relocation")
    module.repair_opencv_runtime_paths(runtime)

    binary_globals = {
        "os": os,
        "__file__": str(cv2_root / "__init__.py"),
        "BINARIES_PATHS": [],
    }
    exec((cv2_root / "config.py").read_text(encoding="utf-8"), binary_globals)
    extension_globals = {
        "os": os,
        "__file__": str(cv2_root / "__init__.py"),
        "PYTHON_EXTENSIONS_PATHS": [],
    }
    exec(
        (cv2_root / "config-3.py").read_text(encoding="utf-8"),
        extension_globals,
    )

    assert binary_globals["BINARIES_PATHS"] == [
        str(runtime / "Library" / "bin")
    ]
    assert extension_globals["PYTHON_EXTENSIONS_PATHS"] == [
        str(cv2_root / "python-3")
    ]
    assert stale_root not in (cv2_root / "config.py").read_text(encoding="utf-8")
    assert stale_root not in (cv2_root / "config-3.py").read_text(encoding="utf-8")
