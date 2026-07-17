from __future__ import annotations

import importlib
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class SfmBackendSelection:
    backend: str
    requested_device: str
    effective_device: str
    gpu_index: str
    gpu_name: str | None
    cpu_fallback: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def hidden_process_options() -> dict[str, int]:
    """返回 SfM 辅助进程的 Windows 隐藏窗口参数。"""
    if os.name != "nt":
        return {}
    return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0))}


def _run_probe(command: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(item) for item in command],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def colmap_command_prefix(executable: str | Path) -> list[str]:
    path = str(executable)
    if Path(path).suffix.lower() in {".bat", ".cmd"} and os.name == "nt":
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", path]
    return [path]


def build_colmap_process_command(executable: str | Path, arguments: Sequence[str]) -> list[str]:
    path = str(executable)
    args = [str(item) for item in arguments]
    if Path(path).suffix.lower() in {".bat", ".cmd"} and os.name == "nt":
        command_line = subprocess.list2cmdline([path, *args])
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]
    return [path, *args]


def find_colmap_executable(
    explicit: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> Path | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(str(explicit))
    if os.environ.get("COLMAP_EXE"):
        candidates.append(str(os.environ["COLMAP_EXE"]))
    for name in ("COLMAP.bat", "colmap.exe", "colmap"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[2]
    local_root = root / ".local" / "colmap"
    if local_root.is_dir():
        for pattern in ("COLMAP.bat", "colmap.exe"):
            candidates.extend(str(path) for path in sorted(local_root.rglob(pattern)))
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def _probe_pycolmap() -> tuple[bool, str | None, bool]:
    try:
        module = importlib.import_module("pycolmap")
    except ModuleNotFoundError:
        return False, None, False
    version = str(getattr(module, "__version__", "unknown"))
    cuda_value = getattr(module, "has_cuda", False)
    cuda_available = bool(cuda_value() if callable(cuda_value) else cuda_value)
    return True, version, cuda_available


def _probe_gpus() -> list[str]:
    completed = _run_probe(
        ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader,nounits"],
        timeout=8.0,
    )
    if completed is None or completed.returncode != 0:
        return []
    names: list[str] = []
    for line in completed.stdout.splitlines():
        parts = [item.strip() for item in line.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            names.append(parts[1])
    return names


def _cuda_build_confirmed(text: str) -> bool:
    lowered = text.lower()
    negative = (
        "without cuda",
        "cuda disabled",
        "cuda unavailable",
        "no cuda support",
    )
    if any(marker in lowered for marker in negative):
        return False
    patterns = (
        r"cuda\s+(?:enabled|support(?:ed)?|version)",
        r"with\s+cuda",
        r"cuda-enabled",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def detect_sfm_environment(
    colmap_exe: str | Path | None = None,
    *,
    requested_device: str = "auto",
) -> dict[str, Any]:
    py_available, py_version, py_cuda = _probe_pycolmap()
    executable = find_colmap_executable(colmap_exe)
    version_text = ""
    help_text = ""
    gpu_option_help = ""
    if executable is not None:
        for args in (("--version",), ("-h",)):
            completed = _run_probe(build_colmap_process_command(executable, args))
            if completed is not None:
                combined = "\n".join([completed.stdout, completed.stderr]).strip()
                if args == ("--version",) and combined:
                    version_text = combined.splitlines()[0]
                help_text += "\n" + combined
        for subcommand in ("feature_extractor", "sequential_matcher"):
            completed = _run_probe(build_colmap_process_command(executable, (subcommand, "-h")))
            if completed is not None and completed.returncode == 0:
                gpu_option_help += "\n" + "\n".join([completed.stdout, completed.stderr])
    gpu_names = _probe_gpus()
    gpu_options_available = (
        ("--FeatureExtraction.use_gpu" in gpu_option_help or "--SiftExtraction.use_gpu" in gpu_option_help)
        and ("--FeatureMatching.use_gpu" in gpu_option_help or "--SiftMatching.use_gpu" in gpu_option_help)
    )
    cli_cuda = bool(executable) and bool(gpu_names) and (
        _cuda_build_confirmed("\n".join([version_text, help_text, gpu_option_help]))
        or gpu_options_available
    )
    if executable and cli_cuda and gpu_names:
        recommended = "colmap_cli_cuda"
    elif py_available and py_cuda and gpu_names:
        recommended = "pycolmap_cuda"
    elif executable:
        recommended = "colmap_cli_cpu"
    elif py_available:
        recommended = "pycolmap_cpu"
    else:
        recommended = "unavailable"
    return {
        "pycolmap_available": py_available,
        "pycolmap_version": py_version,
        "pycolmap_cuda_requested": requested_device in {"auto", "cuda"},
        "pycolmap_cuda_available": py_cuda,
        "colmap_cli_available": executable is not None,
        "colmap_path": str(executable) if executable else None,
        "colmap_version": version_text or None,
        "colmap_cuda_confirmed": cli_cuda,
        "colmap_gpu_options_available": gpu_options_available,
        "cuda_device_available": bool(gpu_names),
        "gpu_names": gpu_names,
        "recommended_backend": recommended,
    }


def select_sfm_backend(
    environment: Mapping[str, Any],
    *,
    backend: str,
    device: str,
    gpu_index: str,
    allow_cpu_fallback: bool = True,
) -> SfmBackendSelection:
    if backend not in {"auto", "pycolmap", "colmap_cli"}:
        raise ValueError(f"unsupported SfM backend: {backend}")
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"unsupported SfM device: {device}")

    cli_available = bool(environment.get("colmap_cli_available"))
    py_available = bool(environment.get("pycolmap_available"))
    has_gpu = bool(environment.get("cuda_device_available"))
    cli_cuda = cli_available and bool(environment.get("colmap_cuda_confirmed")) and has_gpu
    py_cuda = py_available and bool(environment.get("pycolmap_cuda_available")) and has_gpu
    warnings: list[str] = []

    selected = backend
    effective = "cpu"
    if backend == "auto":
        if device != "cpu" and cli_cuda:
            selected, effective = "colmap_cli", "cuda"
        elif device != "cpu" and py_cuda:
            selected, effective = "pycolmap", "cuda"
        elif cli_available:
            selected = "colmap_cli"
        elif py_available:
            selected = "pycolmap"
        else:
            raise RuntimeError("neither official COLMAP CLI nor pycolmap is available")
    elif backend == "colmap_cli":
        if not cli_available:
            raise RuntimeError("COLMAP CLI not found; provide --colmap-exe or set COLMAP_EXE")
        effective = "cuda" if device != "cpu" and cli_cuda else "cpu"
    else:
        if not py_available:
            raise RuntimeError("pycolmap not installed")
        effective = "cuda" if device != "cpu" and py_cuda else "cpu"

    requested_cuda = device == "cuda" or (device == "auto" and effective != "cuda")
    cpu_fallback = effective == "cpu" and device != "cpu"
    if requested_cuda and effective != "cuda":
        message = "CUDA was requested or preferred but could not be verified; using CPU fallback"
        if not allow_cpu_fallback:
            raise RuntimeError(message)
        warnings.append(message)

    gpu_names = list(environment.get("gpu_names") or [])
    return SfmBackendSelection(
        backend=selected,
        requested_device=device,
        effective_device=effective,
        gpu_index=str(gpu_index),
        gpu_name=gpu_names[int(gpu_index)] if effective == "cuda" and str(gpu_index).isdigit() and int(gpu_index) < len(gpu_names) else None,
        cpu_fallback=cpu_fallback,
        warnings=warnings,
    )
