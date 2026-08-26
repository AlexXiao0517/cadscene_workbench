"""构建 Windows 离线测试包的可测试基础工具。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from typing import Sequence
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


@dataclass(frozen=True)
class ReleaseConfig:
    """固定离线包名称和外部算法版本。"""

    bundle_name: str
    application_version: str
    poc_commit: str
    opengv_commit: str

    @classmethod
    def load(cls, path: str | Path) -> "ReleaseConfig":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            bundle_name=str(payload["bundle_name"]),
            application_version=str(payload["application_version"]),
            poc_commit=str(payload["poc_commit"]),
            opengv_commit=str(payload["opengv_commit"]),
        )


_FORBIDDEN_TOP_LEVEL = {".git", "projects", "data", "runs", "tests"}
_NATIVE_RUNTIME_NAMES = ("libc++.dll", "libunwind.dll")


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
    toolchain_bins = sorted(
        (backend / "outputs" / "toolchains").glob(
            "llvm-mingw-*-ucrt-x86_64/bin"
        ),
        reverse=True,
    )
    if toolchain_bins:
        for name in _NATIVE_RUNTIME_NAMES:
            runtime = toolchain_bins[0] / name
            if runtime.is_file():
                selected.add(runtime.relative_to(backend))
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


def prepare_runtime_commands(
    *,
    conda: Path,
    conda_pack: Path,
    source_prefix: Path,
    build_prefix: Path,
    wheel: Path,
    runtime_archive: Path,
) -> tuple[tuple[str, ...], ...]:
    """生成构建专用环境所需的可审计命令序列。"""

    # 主环境的 conda-pack 可能被其他 setuptools/backports 安装破坏；
    # 参数继续保留以兼容旧调用，但实际打包器固定安装在一次性构建环境中。
    _ = conda_pack
    python = build_prefix / "python.exe"
    return (
        (
            str(conda),
            "create",
            "--yes",
            "--prefix",
            str(build_prefix),
            "--clone",
            str(source_prefix),
        ),
        (str(python), "-m", "pip", "install", str(wheel)),
        (str(python), "-m", "pip", "check"),
        (
            str(python),
            "-c",
            "import av, cv2, numpy, scipy, yaml, PIL, ezdxf, imageio_ffmpeg, pycolmap, psutil",
        ),
        (
            str(python),
            "-m",
            "pip",
            "install",
            "conda-pack==0.9.2",
        ),
        (
            str(build_prefix / "Scripts" / "conda-pack.exe"),
            "--prefix",
            str(build_prefix),
            "--output",
            str(runtime_archive),
            "--force",
        ),
    )


def _safe_extract_tar(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != resolved_destination and resolved_destination not in target.parents:
                raise ValueError(f"runtime archive escapes destination: {member.name}")
        archive.extractall(destination, members=members)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def assemble_bundle(
    *,
    config: ReleaseConfig,
    runtime_archive: str | Path,
    backend_root: str | Path,
    templates_root: str | Path,
    output_dir: str | Path,
    source_commit: str,
) -> Path:
    """从运行环境归档和后端白名单组装未压缩交付目录。"""

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    layout = BundleLayout(output / config.bundle_name)
    if layout.root.exists():
        shutil.rmtree(layout.root)
    layout.root.mkdir(parents=True)
    _safe_extract_tar(Path(runtime_archive), layout.runtime)

    templates = Path(templates_root)
    for item in templates.iterdir():
        if item.name == "release-config.json":
            continue
        destination = layout.root / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        elif item.is_file():
            shutil.copy2(item, destination)

    backend = Path(backend_root)
    for relative in backend_runtime_files(backend):
        source = backend / relative
        if relative.name in _NATIVE_RUNTIME_NAMES:
            # Windows 先从可执行文件目录加载依赖 DLL；与 CLI 同目录可避免
            # 启动器 PATH 变化或系统中同名运行库导致的加载失败。
            destination = (
                layout.backend / "outputs" / "build_opengv_cli" / relative.name
            )
        else:
            destination = layout.backend / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    layout.backend.mkdir(parents=True, exist_ok=True)
    (layout.backend / "backend_version.json").write_text(
        json.dumps(
            {
                "poc_commit": config.poc_commit,
                "opengv_commit": config.opengv_commit,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    layout.launcher.mkdir(parents=True, exist_ok=True)
    native_runtime = tuple(
        layout.backend / "outputs" / "build_opengv_cli" / name
        for name in _NATIVE_RUNTIME_NAMES
    )
    critical = (
        layout.runtime / "python.exe",
        layout.runtime / "Scripts" / "conda-unpack.exe",
        layout.backend / "scripts" / "run_full_video_exploration.py",
        layout.backend / "outputs" / "build_opengv_cli" / "opengv_rotation_cli.exe",
        *native_runtime,
    )
    manifest = {
        "bundle_name": config.bundle_name,
        "application_version": config.application_version,
        "source_commit": source_commit,
        "poc_commit": config.poc_commit,
        "opengv_commit": config.opengv_commit,
        "critical_sha256": {
            path.relative_to(layout.root).as_posix(): _sha256(path)
            for path in critical
            if path.is_file()
        },
    }
    (layout.launcher / "release.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    validate_staging_tree(layout.root)
    verify_bundle(layout.root, config=config)
    return layout.root


def verify_bundle(root: str | Path, *, config: ReleaseConfig) -> None:
    """验证离线包关键运行文件、版本固定和排除规则。"""

    layout = BundleLayout(Path(root))
    required = (
        layout.runtime / "python.exe",
        layout.runtime / "Scripts" / "conda-unpack.exe",
        layout.backend / "scripts" / "run_full_video_exploration.py",
        layout.backend / "outputs" / "build_opengv_cli" / "opengv_rotation_cli.exe",
        layout.root / "启动CAD视频工作台.cmd",
        layout.root / "关闭CAD视频工作台.cmd",
        layout.launcher / "start.ps1",
        layout.launcher / "stop.ps1",
        layout.launcher / "release.json",
    )
    for path in required:
        if not path.is_file():
            raise ValueError(f"required bundle file is missing: {path.relative_to(layout.root)}")
    for name in _NATIVE_RUNTIME_NAMES:
        path = layout.backend / "outputs" / "build_opengv_cli" / name
        if not path.is_file():
            raise ValueError(f"required bundle file is missing: {name}")
    version_path = layout.backend / "backend_version.json"
    if not version_path.is_file():
        raise ValueError("required bundle file is missing: pure_rotation_backend/backend_version.json")
    version = json.loads(version_path.read_text(encoding="utf-8"))
    expected = {
        "poc_commit": config.poc_commit,
        "opengv_commit": config.opengv_commit,
    }
    if version != expected:
        raise ValueError("pure rotation backend version does not match release config")
    validate_staging_tree(layout.root)


def _run_commands(commands: Sequence[Sequence[str]]) -> None:
    for command in commands:
        completed = subprocess.run(list(command), check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"command failed with exit code {completed.returncode}: {command[0]}"
            )


def build_parser() -> argparse.ArgumentParser:
    repository_root = Path(__file__).resolve().parents[1]
    default_config = repository_root / "packaging" / "windows" / "release-config.json"
    parser = argparse.ArgumentParser(description="Build the Windows offline tester bundle.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-runtime")
    prepare.add_argument("--conda", required=True, type=Path)
    prepare.add_argument("--conda-pack", required=True, type=Path)
    prepare.add_argument("--source-prefix", required=True, type=Path)
    prepare.add_argument("--build-prefix", required=True, type=Path)
    prepare.add_argument("--wheel", required=True, type=Path)
    prepare.add_argument("--runtime-archive", required=True, type=Path)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--config", default=default_config, type=Path)
    assemble.add_argument("--runtime-archive", required=True, type=Path)
    assemble.add_argument("--backend-root", required=True, type=Path)
    assemble.add_argument("--templates-root", default=repository_root / "packaging" / "windows", type=Path)
    assemble.add_argument("--output-dir", required=True, type=Path)
    assemble.add_argument("--source-commit", required=True)
    assemble.add_argument("--zip", action="store_true")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--config", default=default_config, type=Path)
    verify.add_argument("--bundle-root", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare-runtime":
        commands = prepare_runtime_commands(
            conda=args.conda,
            conda_pack=args.conda_pack,
            source_prefix=args.source_prefix,
            build_prefix=args.build_prefix,
            wheel=args.wheel,
            runtime_archive=args.runtime_archive,
        )
        _run_commands(commands)
        return 0
    config = ReleaseConfig.load(args.config)
    if args.command == "verify":
        verify_bundle(args.bundle_root, config=config)
        return 0
    bundle = assemble_bundle(
        config=config,
        runtime_archive=args.runtime_archive,
        backend_root=args.backend_root,
        templates_root=args.templates_root,
        output_dir=args.output_dir,
        source_commit=args.source_commit,
    )
    if args.zip:
        write_zip64(bundle, bundle.parent / f"{bundle.name}.zip")
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
