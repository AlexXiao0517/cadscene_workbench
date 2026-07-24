from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from cadscene.sfm.backend_detection import build_colmap_process_command, hidden_process_options


@dataclass(frozen=True)
class ColmapCliPaths:
    images_dir: Path
    masks_dir: Path
    database_path: Path
    sparse_dir: Path


@dataclass(frozen=True)
class ColmapCommandResult:
    command: list[str]
    returncode: int
    output: str
    elapsed_sec: float


class ColmapCommandError(RuntimeError):
    def __init__(self, result: ColmapCommandResult) -> None:
        self.result = result
        super().__init__(
            f"COLMAP command failed with return code {result.returncode}: {' '.join(result.command)}\n{result.output}"
        )


@dataclass(frozen=True)
class ColmapTextModelExport:
    trajectory: dict
    intrinsics: list[dict]
    points: np.ndarray
    colors: np.ndarray | None
    mean_reprojection_error: float | None


def _option(help_text: str, candidates: Sequence[str], fallback: str) -> str:
    return next((name for name in candidates if name in help_text), fallback)


def build_colmap_cli_commands(
    executable: str | Path,
    paths: ColmapCliPaths,
    *,
    camera_model: str,
    max_image_size: int,
    max_num_features: int,
    sequential_overlap: int,
    init_min_tri_angle: float,
    use_mask: bool,
    use_gpu: bool,
    gpu_index: str,
    feature_help: str,
    matching_help: str,
) -> tuple[list[str], list[str], list[str]]:
    extraction_gpu = _option(
        feature_help,
        ("--FeatureExtraction.use_gpu", "--SiftExtraction.use_gpu"),
        "--FeatureExtraction.use_gpu",
    )
    extraction_index = _option(
        feature_help,
        ("--FeatureExtraction.gpu_index", "--SiftExtraction.gpu_index"),
        "--FeatureExtraction.gpu_index",
    )
    max_size = _option(
        feature_help,
        ("--FeatureExtraction.max_image_size", "--SiftExtraction.max_image_size"),
        "--FeatureExtraction.max_image_size",
    )
    max_features = _option(
        feature_help,
        ("--FeatureExtraction.max_num_features", "--SiftExtraction.max_num_features"),
        "--FeatureExtraction.max_num_features",
    )
    matching_gpu = _option(
        matching_help,
        ("--FeatureMatching.use_gpu", "--SiftMatching.use_gpu"),
        "--FeatureMatching.use_gpu",
    )
    matching_index = _option(
        matching_help,
        ("--FeatureMatching.gpu_index", "--SiftMatching.gpu_index"),
        "--FeatureMatching.gpu_index",
    )
    feature_args = [
        "feature_extractor",
        "--database_path",
        str(paths.database_path),
        "--image_path",
        str(paths.images_dir),
        "--ImageReader.single_camera",
        "1",
        "--ImageReader.camera_model",
        str(camera_model),
        max_size,
        str(max_image_size),
        max_features,
        str(max_num_features),
        extraction_gpu,
        "1" if use_gpu else "0",
        extraction_index,
        str(gpu_index),
    ]
    if use_mask:
        feature_args.extend(["--ImageReader.mask_path", str(paths.masks_dir)])
    matching_args = [
        "sequential_matcher",
        "--database_path",
        str(paths.database_path),
        "--SequentialMatching.overlap",
        str(sequential_overlap),
        matching_gpu,
        "1" if use_gpu else "0",
        matching_index,
        str(gpu_index),
    ]
    mapper_args = [
        "mapper",
        "--database_path",
        str(paths.database_path),
        "--image_path",
        str(paths.images_dir),
        "--output_path",
        str(paths.sparse_dir),
        "--Mapper.init_min_tri_angle",
        str(init_min_tri_angle),
    ]
    return (
        build_colmap_process_command(executable, feature_args),
        build_colmap_process_command(executable, matching_args),
        build_colmap_process_command(executable, mapper_args),
    )


def parse_gpu_execution(output: str, *, requested: bool) -> bool:
    if not requested:
        return False
    lowered = str(output).lower()
    negative = (
        "falling back to cpu",
        "fallback to cpu",
        "without cuda",
        "cuda unavailable",
        "no cuda-enabled device",
        "failed to load cuda",
    )
    if any(marker in lowered for marker in negative):
        return False
    positive = (
        r"using\s+cuda",
        r"cuda\s+device",
        r"sift\s*gpu",
        r"gpu\s+feature\s+(?:extractor|matcher)",
        r"bind\s+feature(?:extractor|matcher)worker\s+to\s+gpu\s+device",
    )
    return any(re.search(pattern, lowered) for pattern in positive)


def is_cuda_failure(output: str) -> bool:
    lowered = str(output).lower()
    markers = (
        "compiled without cuda",
        "cuda unavailable",
        "no cuda-enabled device",
        "no cuda device",
        "failed to load cuda",
        "siftgpu not supported",
        "cuda driver version is insufficient",
    )
    return any(marker in lowered for marker in markers)


def run_colmap_command(
    command: Sequence[str],
    *,
    popen_factory: Callable = subprocess.Popen,
) -> ColmapCommandResult:
    command_list = [str(item) for item in command]
    started = time.perf_counter()
    process = popen_factory(
        command_list,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        **hidden_process_options(),
    )
    lines: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            print(line, end="", flush=True)
            lines.append(line)
    returncode = int(process.wait())
    result = ColmapCommandResult(
        command=command_list,
        returncode=returncode,
        output="".join(lines),
        elapsed_sec=time.perf_counter() - started,
    )
    if returncode != 0:
        raise ColmapCommandError(result)
    return result


def probe_colmap_subcommand_help(executable: str | Path, subcommand: str) -> str:
    command = build_colmap_process_command(executable, (str(subcommand), "-h"))
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
            **hidden_process_options(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to inspect COLMAP {subcommand} options: {exc}") from exc
    output = "\n".join([completed.stdout, completed.stderr])
    if completed.returncode != 0:
        raise RuntimeError(f"COLMAP {subcommand} -h failed with return code {completed.returncode}")
    return output


def run_colmap_cli_pipeline(
    executable: str | Path,
    paths: ColmapCliPaths,
    *,
    camera_model: str,
    max_image_size: int,
    max_num_features: int,
    sequential_overlap: int,
    init_min_tri_angle: float,
    use_mask: bool,
    use_gpu: bool,
    gpu_index: str,
    progress_callback: Callable[[str, float, str], None] | None = None,
) -> dict:
    feature_help = probe_colmap_subcommand_help(executable, "feature_extractor")
    matching_help = probe_colmap_subcommand_help(executable, "sequential_matcher")
    commands = build_colmap_cli_commands(
        executable,
        paths,
        camera_model=camera_model,
        max_image_size=max_image_size,
        max_num_features=max_num_features,
        sequential_overlap=sequential_overlap,
        init_min_tri_angle=init_min_tri_angle,
        use_mask=use_mask,
        use_gpu=use_gpu,
        gpu_index=gpu_index,
        feature_help=feature_help,
        matching_help=matching_help,
    )
    names = ("feature_extraction", "feature_matching", "mapper")
    progress_values = (0.32, 0.52, 0.72)
    messages = ("正在提取特征", "正在进行顺序匹配", "正在稀疏重建")
    results: dict[str, ColmapCommandResult] = {}
    for name, progress, message, command in zip(names, progress_values, messages, commands):
        if progress_callback:
            progress_callback(name, progress, message)
        results[name] = run_colmap_command(command)
    return {
        "commands": {name: result.command for name, result in results.items()},
        "timings": {f"{name}_sec": result.elapsed_sec for name, result in results.items()},
        "logs": {name: result.output for name, result in results.items()},
        "feature_extraction_gpu": parse_gpu_execution(results["feature_extraction"].output, requested=use_gpu),
        "feature_matching_gpu": parse_gpu_execution(results["feature_matching"].output, requested=use_gpu),
    }


def convert_colmap_model_to_text(
    executable: str | Path,
    model_dir: str | Path,
    output_dir: str | Path,
) -> Path:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    command = build_colmap_process_command(
        executable,
        [
        "model_converter",
        "--input_path",
        str(model_dir),
        "--output_path",
        str(target),
        "--output_type",
        "TXT",
        ],
    )
    run_colmap_command(command)
    return target


def first_sparse_model_dir(sparse_dir: str | Path) -> Path:
    root = Path(sparse_dir)
    candidates = sorted(
        [item for item in root.iterdir() if item.is_dir()],
        key=lambda item: (not item.name.isdigit(), int(item.name) if item.name.isdigit() else item.name),
    ) if root.exists() else []
    if candidates:
        return candidates[0]
    if (root / "cameras.bin").exists() or (root / "cameras.txt").exists():
        return root
    raise RuntimeError(f"COLMAP mapper produced no sparse model in: {root}")


def _rotation_from_quaternion_wxyz(quaternion: Sequence[float]) -> np.ndarray:
    w, x, y, z = (float(value) for value in quaternion)
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float64,
    )


def export_colmap_text_model(
    model_dir: str | Path,
    *,
    frame_indices: Sequence[int],
    fps: float,
    width: int,
    height: int,
    video_path: str | Path | None,
) -> ColmapTextModelExport:
    """将官方 COLMAP TXT 模型转换成 cadscene 已有输出协议。"""
    root = Path(model_dir)
    intrinsics: list[dict] = []
    for line in (root / "cameras.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        intrinsics.append(
            {
                "camera_id": int(fields[0]),
                "model": fields[1],
                "width": int(fields[2]),
                "height": int(fields[3]),
                "params": [float(value) for value in fields[4:]],
            }
        )

    image_lines = [
        line.strip()
        for line in (root / "images.txt").read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    images: dict[str, dict] = {}
    index = 0
    while index < len(image_lines):
        header = image_lines[index]
        points_line = image_lines[index + 1] if index + 1 < len(image_lines) else ""
        index += 2
        if not header:
            continue
        fields = header.split()
        if len(fields) < 10:
            continue
        quaternion = [float(value) for value in fields[1:5]]
        translation = np.asarray([float(value) for value in fields[5:8]], dtype=np.float64)
        rotation = _rotation_from_quaternion_wxyz(quaternion)
        point_fields = points_line.split()
        observations = sum(
            1
            for item_index in range(2, len(point_fields), 3)
            if int(float(point_fields[item_index])) >= 0
        )
        images[fields[9]] = {
            "center": (-rotation.T @ translation).tolist(),
            "quaternion": quaternion,
            "num_observations": observations,
        }

    poses: list[dict] = []
    for frame_index in frame_indices:
        name = f"frame_{int(frame_index):06d}.png"
        image = images.get(name)
        if image is None:
            poses.append({"frame_index": int(frame_index), "registered": False})
        else:
            poses.append(
                {
                    "frame_index": int(frame_index),
                    "registered": True,
                    "center": image["center"],
                    "cam_from_world_quat_wxyz": image["quaternion"],
                    "num_observations": int(image["num_observations"]),
                }
            )

    points: list[list[float]] = []
    colors: list[list[int]] = []
    errors: list[float] = []
    for line in (root / "points3D.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 8:
            continue
        points.append([float(value) for value in fields[1:4]])
        colors.append([int(value) for value in fields[4:7]])
        errors.append(float(fields[7]))
    xyz = np.asarray(points, dtype=np.float64).reshape((-1, 3))
    rgb = np.asarray(colors, dtype=np.uint8).reshape((-1, 3)) if colors else None
    trajectory = {
        "schema_version": "cadscene_sfm_trajectory_v1",
        "source": "cadscene.run_sfm",
        "video": str(video_path) if video_path is not None else None,
        "fps": float(fps),
        "width": int(width),
        "height": int(height),
        "intrinsics": intrinsics,
        "poses": poses,
    }
    return ColmapTextModelExport(
        trajectory=trajectory,
        intrinsics=intrinsics,
        points=xyz,
        colors=rgb,
        mean_reprojection_error=float(np.mean(errors)) if errors else None,
    )
