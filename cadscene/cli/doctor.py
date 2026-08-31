from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

from cadscene.application_resources import application_root, validate_application_root
from cadscene.pure_rotation.backend import ExternalOpenGVBackend


@dataclass(frozen=True)
class DoctorOptions:
    application_root: Path
    storage_root: Path
    pure_rotation_backend_root: Path | None = None
    pure_rotation_python: Path | None = None
    pure_rotation_calibration_root: Path | None = None


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    group: str
    status: str
    required: bool
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def complete_capability(self) -> bool:
        return all(item.status == "ok" for item in self.checks if item.required)

    def to_dict(self) -> dict[str, object]:
        return {
            "complete_capability": self.complete_capability,
            "checks": [asdict(item) for item in self.checks],
        }


@dataclass(frozen=True)
class DoctorProbes:
    module_available: Callable[[str], bool]
    executable_path: Callable[[str], str | None]
    resources_check: Callable[[Path], tuple[bool, str]]
    storage_check: Callable[[Path], tuple[bool, str]]
    pure_rotation_check: Callable[[DoctorOptions], tuple[bool, str]]
    module_importable: Callable[[str], tuple[bool, str]] | None = None


_CORE_MODULES = (
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("PyYAML", "yaml"),
    ("Pillow", "PIL"),
    ("OpenCV", "cv2"),
    ("ezdxf", "ezdxf"),
    ("imageio-ffmpeg", "imageio_ffmpeg"),
    ("psutil", "psutil"),
)


def run_doctor(
    options: DoctorOptions,
    *,
    probes: DoctorProbes | None = None,
) -> DoctorReport:
    active = probes or default_probes()
    checks: list[DoctorCheck] = []
    for display_name, module_name in _CORE_MODULES:
        available, message = _module_check(active, module_name)
        checks.append(_check(display_name, "core", available, message))

    resources_ok, resources_message = active.resources_check(options.application_root)
    checks.append(_check("application_resources", "core", resources_ok, resources_message))
    for executable in ("ffmpeg", "ffprobe"):
        path = active.executable_path(executable)
        checks.append(_check(executable, "media", bool(path), path or f"{executable} not found"))

    pycolmap_ok, pycolmap_message = _module_check(active, "pycolmap")
    checks.append(_check("pycolmap", "sfm", pycolmap_ok, pycolmap_message))
    rotation_ok, rotation_message = active.pure_rotation_check(options)
    checks.append(_check("opengv_backend", "pure_rotation", rotation_ok, rotation_message))
    storage_ok, storage_message = active.storage_check(options.storage_root)
    checks.append(_check("storage_root", "storage", storage_ok, storage_message))
    return DoctorReport(tuple(checks))


def _check(name: str, group: str, ok: bool, message: str) -> DoctorCheck:
    return DoctorCheck(name, group, "ok" if ok else "error", True, message)


def _module_check(probes: DoctorProbes, name: str) -> tuple[bool, str]:
    if probes.module_importable is not None:
        return probes.module_importable(name)
    available = probes.module_available(name)
    return available, f"Python module {name}" if available else f"Python module {name} not found"


def default_probes() -> DoctorProbes:
    return DoctorProbes(
        module_available=lambda name: importlib.util.find_spec(name) is not None,
        executable_path=shutil.which,
        resources_check=_resources_check,
        storage_check=_storage_check,
        pure_rotation_check=_pure_rotation_check,
        module_importable=_module_import_check,
    )


def _module_import_check(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
    except Exception as exc:
        return False, f"unable to import {name}: {exc}"
    return True, f"imported {name}"


def _resources_check(root: Path) -> tuple[bool, str]:
    try:
        validate_application_root(root)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return True, "official apps and configs found"


def _storage_check(root: Path) -> tuple[bool, str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".cadscene-doctor-", dir=root, delete=True):
            pass
    except OSError as exc:
        return False, f"storage root is not writable: {exc}"
    return True, f"storage root is writable: {root.resolve()}"


def _pure_rotation_check(options: DoctorOptions) -> tuple[bool, str]:
    from cadscene.cli.serve_viewer import (
        resolve_pure_rotation_calibration_root,
        resolve_pure_rotation_runtime,
    )

    backend_root, python_path = resolve_pure_rotation_runtime(
        backend_root=str(options.pure_rotation_backend_root) if options.pure_rotation_backend_root else None,
        backend_python=str(options.pure_rotation_python) if options.pure_rotation_python else None,
        search_from=options.application_root,
        executable=Path(sys.executable),
    )
    calibration_root = options.pure_rotation_calibration_root or resolve_pure_rotation_calibration_root(
        configured=None,
        application_root=options.application_root,
    )
    if backend_root is None:
        return False, "pinned OpenGV backend is unavailable"
    health = ExternalOpenGVBackend(backend_root=backend_root).health_check()
    if not health.get("available"):
        return False, str(health.get("message") or "pinned OpenGV backend is unavailable")
    if python_path is None or not python_path.is_file():
        return False, "pure-rotation Python environment is unavailable"
    if calibration_root is None or not (calibration_root / "cameras.txt").is_file():
        return False, "pure-rotation calibration is unavailable"
    try:
        completed = subprocess.run(
            [str(python_path), "-c", "import av, cv2, numpy, scipy, yaml"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pure-rotation Python probe failed: {exc}"
    if completed.returncode != 0:
        return False, "pure-rotation Python dependencies are incomplete"
    return True, "pinned OpenGV backend, calibration, and Python environment found"


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Check the complete cadscene-workbench environment.")
    parser.add_argument("--application-root", default=str(root))
    parser.add_argument("--storage-root", default=str(root))
    parser.add_argument("--pure-rotation-backend-root")
    parser.add_argument("--pure-rotation-python")
    parser.add_argument("--pure-rotation-calibration-root")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = DoctorOptions(
        application_root=Path(args.application_root).resolve(),
        storage_root=Path(args.storage_root).resolve(),
        pure_rotation_backend_root=Path(args.pure_rotation_backend_root).resolve() if args.pure_rotation_backend_root else None,
        pure_rotation_python=Path(args.pure_rotation_python).resolve() if args.pure_rotation_python else None,
        pure_rotation_calibration_root=Path(args.pure_rotation_calibration_root).resolve() if args.pure_rotation_calibration_root else None,
    )
    report = run_doctor(options)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for item in report.checks:
            marker = "OK" if item.status == "ok" else "ERROR"
            print(f"[{marker}] {item.group}/{item.name}: {item.message}")
        print("Environment ready." if report.complete_capability else "Environment is incomplete.")
    return 0 if report.complete_capability else 1


if __name__ == "__main__":
    raise SystemExit(main())
