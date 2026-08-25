"""构建 Windows 离线测试包的可测试基础工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import zipfile


@dataclass(frozen=True)
class BundleLayout:
    """离线包中只读运行时与可写数据目录的固定布局。"""

    root: Path

    @property
    def runtime(self) -> Path:
        return self.root / "runtime"

    @property
    def backend(self) -> Path:
        return self.root / "pure_rotation_backend"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def launcher(self) -> Path:
        return self.root / "launcher"


_FORBIDDEN_TOP_LEVEL = {".git", "projects", "data", "runs", "tests"}


def validate_staging_tree(root: str | Path) -> None:
    """拒绝误装入交付包的源码状态、用户数据和历史日志。"""

    resolved = Path(root)
    if not resolved.is_dir():
        raise ValueError(f"bundle staging root does not exist: {resolved}")
    for path in resolved.rglob("*"):
        relative = path.relative_to(resolved)
        if relative.parts and relative.parts[0].lower() in _FORBIDDEN_TOP_LEVEL:
            raise ValueError(f"forbidden bundle content: {relative.as_posix()}")
        if ".git" in {part.lower() for part in relative.parts}:
            raise ValueError(f"forbidden bundle content: {relative.as_posix()}")
        if path.is_file() and (
            path.suffix.lower() == ".log"
            or (relative.parts and relative.parts[0].lower() == "logs")
        ):
            raise ValueError(f"forbidden bundle content: {relative.as_posix()}")


def backend_runtime_files(root: str | Path) -> tuple[Path, ...]:
    """返回固定 Pure Rotation 后端中允许交付的相对文件。"""

    backend = Path(root)
    selected: set[Path] = set()
    source = backend / "src"
    if source.is_dir():
        selected.update(path.relative_to(backend) for path in source.rglob("*.py") if path.is_file())
    for relative in (
        Path("scripts/run_full_video_exploration.py"),
        Path("outputs/build_opengv_cli/opengv_rotation_cli.exe"),
        Path("backend_version.json"),
    ):
        if (backend / relative).is_file():
            selected.add(relative)
    return tuple(sorted(selected, key=lambda value: value.as_posix()))


def write_zip64(source: str | Path, output: str | Path) -> Path:
    """以稳定成员顺序写入含单一顶层目录的 ZIP64 文件。"""

    root = Path(source)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda value: value.relative_to(root).as_posix(),
        ):
            member = Path(root.name) / path.relative_to(root)
            archive.write(path, member.as_posix())
    return destination

