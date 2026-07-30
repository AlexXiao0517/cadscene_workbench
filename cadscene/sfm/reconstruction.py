from __future__ import annotations

import importlib
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from cadscene.core.io import ensure_dir, read_json, write_csv_utf8_sig, write_json, write_text
from cadscene.sfm.backend_detection import detect_sfm_environment, select_sfm_backend
from cadscene.sfm.colmap_cli import (
    ColmapCommandError,
    ColmapCliPaths,
    convert_colmap_model_to_text,
    export_colmap_text_model,
    first_sparse_model_dir,
    is_cuda_failure,
    run_colmap_cli_pipeline,
)
from cadscene.sfm.backend_detection import hidden_process_options


FRAME_SUFFIX = ".png"


@dataclass(frozen=True)
class ReconstructionConfig:
    start_frame: int = 0
    num_frames: int = 0
    frame_step: int = 5
    max_image_size: int = 2048
    max_num_features: int = 12000
    camera_model: str = "OPENCV"
    sequential_overlap: int = 15
    quadratic_overlap: bool = True
    init_min_tri_angle: float = 2.0
    ba_global_frames_ratio: float = 2.0
    ba_global_points_ratio: float = 2.0
    ba_global_frames_freq: int = 1000
    ba_global_points_freq: int = 1_000_000
    ba_global_max_num_iterations: int = 25
    ba_global_max_refinements: int = 2
    init_max_forward_motion: float = 1.0
    filter_min_tri_angle: float = 1.0
    triangulation_min_angle: float = 1.0
    min_num_matches: int = 15
    init_min_num_inliers: int = 50
    multiple_models: bool = True
    min_reg_images: int = 10
    reuse_database: bool = False
    export_only: bool = False
    use_mask: bool = False
    colmap_exe: str | None = None
    backend: str = "pycolmap"
    device: str = "cpu"
    gpu_index: str = "0"
    no_cpu_fallback: bool = False


@dataclass(frozen=True)
class ExtractedFrame:
    frame_index: int
    path: Path
    pts_time_sec: float | None = None
    timestamp_source: str = "unknown"


@dataclass
class ReconstructionResult:
    trajectory: dict
    intrinsics: list[dict]
    points: np.ndarray
    colors: np.ndarray | None
    stats: dict
    warnings: list[str] = field(default_factory=list)
    frame_timestamps: list[dict[str, object]] = field(default_factory=list)


def frame_indices_for_config(config: ReconstructionConfig, frame_count: int) -> list[int]:
    start = max(0, int(config.start_frame))
    step = max(1, int(config.frame_step))
    end = frame_count
    if int(config.num_frames) > 0:
        end = min(frame_count, start + int(config.num_frames)) if frame_count > 0 else start + int(config.num_frames)
    return list(range(start, max(start, end), step))


def frame_image_name(frame_index: int) -> str:
    return f"frame_{int(frame_index):06d}{FRAME_SUFFIX}"


def mask_path_for_frame(seg_dir: str | Path, frame_index: int) -> Path:
    return Path(seg_dir) / frame_image_name(frame_index)


def extract_frames(
    video_path: str | Path,
    images_dir: str | Path,
    frame_indices: Sequence[int],
) -> list[ExtractedFrame]:
    cv2 = importlib.import_module("cv2")
    source = Path(video_path)
    if not source.exists():
        raise FileNotFoundError(f"video does not exist: {source}")
    targets = sorted({int(value) for value in frame_indices})
    if not targets:
        return []
    output = ensure_dir(images_dir)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {source}")
    extracted: list[ExtractedFrame] = []
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, targets[0])
        target_set = set(targets)
        frame_index = targets[0]
        while frame_index <= targets[-1]:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            if frame_index in target_set:
                path = output / frame_image_name(frame_index)
                if not cv2.imwrite(str(path), frame):
                    raise RuntimeError(f"failed to write extracted frame: {path}")
                pts_msec = float(capture.get(cv2.CAP_PROP_POS_MSEC))
                pts_time_sec = pts_msec / 1000.0 if np.isfinite(pts_msec) and pts_msec >= 0.0 else None
                extracted.append(
                    ExtractedFrame(
                        frame_index=frame_index,
                        path=path,
                        pts_time_sec=pts_time_sec,
                        timestamp_source="opencv_pos_msec" if pts_time_sec is not None else "unavailable",
                    )
                )
            frame_index += 1
    finally:
        capture.release()
    return extracted


def frame_timestamp_rows(frames: Sequence[ExtractedFrame]) -> list[dict[str, object]]:
    """Serialize extracted frames without conflating source-frame and extract order."""

    return [
        {
            "source_frame_index": frame.frame_index,
            "extracted_index": extracted_index,
            "image_name": frame.path.name,
            "pts_time_sec": "" if frame.pts_time_sec is None else frame.pts_time_sec,
            "timestamp_source": frame.timestamp_source,
            "cfr_confirmed": False,
        }
        for extracted_index, frame in enumerate(frames)
    ]


def prepare_masks(
    seg_dir: str | Path,
    masks_dir: str | Path,
    frames: Sequence[ExtractedFrame],
) -> tuple[int, list[str]]:
    output = ensure_dir(masks_dir)
    copied = 0
    warnings: list[str] = []
    for frame in frames:
        source = mask_path_for_frame(seg_dir, frame.frame_index)
        if not source.exists():
            warnings.append(f"missing mask for frame {frame.frame_index}: {source}")
            continue
        # COLMAP 对 image.png 使用 image.png.png 作为 mask 名称。
        shutil.copyfile(source, output / f"{frame.path.name}.png")
        copied += 1
    return copied, warnings


def load_pycolmap():
    try:
        return importlib.import_module("pycolmap")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pycolmap not installed. Please run this stage in the difusser "
            "environment or provide --colmap-exe if supported."
        ) from exc


def configure_incremental_mapping_options(options, config: ReconstructionConfig):
    """复刻旧版对航拍前向飞行场景使用的增量重建参数。"""
    options.mergedict(
        {
            "min_num_matches": int(config.min_num_matches),
            "multiple_models": bool(config.multiple_models),
            "min_model_size": int(max(3, config.min_reg_images // 2)),
            "mapper": {
                "init_min_tri_angle": float(config.init_min_tri_angle),
                "init_max_forward_motion": float(config.init_max_forward_motion),
                "init_min_num_inliers": int(config.init_min_num_inliers),
                "filter_min_tri_angle": float(config.filter_min_tri_angle),
            },
            "triangulation": {
                "min_angle": float(config.triangulation_min_angle),
            },
        }
    )
    return options


def configure_pycolmap_feature_options(pycolmap, *, use_gpu: bool, gpu_index: str):
    """显式配置 pycolmap 设备，避免 auto 模式被误认为已启用 CUDA。"""
    extraction_options = pycolmap.FeatureExtractionOptions()
    matching_options = pycolmap.FeatureMatchingOptions()
    extraction_options.use_gpu = bool(use_gpu)
    extraction_options.gpu_index = str(gpu_index)
    matching_options.use_gpu = bool(use_gpu)
    matching_options.gpu_index = str(gpu_index)
    device = pycolmap.Device.cuda if use_gpu else pycolmap.Device.cpu
    return extraction_options, matching_options, device


def load_best_sparse_model(pycolmap, sparse_dir: str | Path):
    candidates = []
    root = Path(sparse_dir)
    if not root.exists():
        raise FileNotFoundError(f"export-only sparse directory does not exist: {root}")
    model_dirs = [item for item in root.iterdir() if item.is_dir()]
    if not model_dirs:
        model_dirs = [root]
    for model_dir in model_dirs:
        try:
            reconstruction = pycolmap.Reconstruction(str(model_dir))
        except Exception:
            continue
        candidates.append(reconstruction)
    if not candidates:
        raise RuntimeError(f"export-only found no readable COLMAP model in: {root}")
    return max(candidates, key=lambda item: int(_value(item.num_reg_images)))


def build_camera_trajectory(
    *,
    fps: float,
    width: int,
    height: int,
    intrinsics: Sequence[Mapping[str, Any]],
    poses: Sequence[Mapping[str, Any]],
    video_path: str | Path | None = None,
) -> dict:
    return {
        "schema_version": "cadscene_sfm_trajectory_v1",
        "source": "cadscene.run_sfm",
        "video": str(video_path) if video_path is not None else None,
        "fps": float(fps),
        "width": int(width),
        "height": int(height),
        "intrinsics": [dict(item) for item in intrinsics],
        "poses": [dict(item) for item in poses],
    }


def build_sfm_stats(
    *,
    frame_count: int,
    extracted_frame_count: int,
    registered_count: int,
    point_count: int,
    mean_reprojection_error: float | None,
    config: ReconstructionConfig,
    backend: str,
    warnings: Sequence[str] = (),
    runtime: Mapping[str, Any] | None = None,
) -> dict:
    runtime_values = dict(runtime or {})
    registered_ratio = float(registered_count / extracted_frame_count) if extracted_frame_count else 0.0
    minimum_point_support = max(100, int(registered_count))
    suitability_reasons: list[str] = []
    if registered_count < 3:
        suitability_reasons.append("registered_frames_too_few")
    if extracted_frame_count and registered_ratio < 0.2:
        suitability_reasons.append("registered_ratio_too_low")
    if point_count < minimum_point_support:
        suitability_reasons.append("sparse_point_support_too_low")
    suitable_for_3d = not suitability_reasons
    low_parallax_suspected = "sparse_point_support_too_low" in suitability_reasons
    warning_items = list(warnings)
    if not suitable_for_3d and low_parallax_suspected:
        warning_items.append(
            "当前 SfM 几何支撑不足，不适合三维重建；可能存在低视差、纯旋转或纹理不足。"
            "请重新拍摄并让无人机产生明显平移，例如沿路线飞行或围绕场地移动。"
        )
    elif not suitable_for_3d:
        warning_items.append(
            "SfM 注册失败或注册率过低，当前轨迹不能进入 CAD 配准。"
            "点云数量较多并不能替代多帧相机注册，且不能据此判断视频缺少平移；"
            "请使用前向飞行参数重新运行 SfM，并检查匹配和相机内参。"
        )
    return {
        "frame_count": int(frame_count),
        "extracted_frame_count": int(extracted_frame_count),
        "registered_count": int(registered_count),
        "registered_ratio": registered_ratio,
        "point_count": int(point_count),
        "points_per_registered_frame": float(point_count / registered_count) if registered_count else 0.0,
        "minimum_point_support": minimum_point_support,
        "suitable_for_3d": suitable_for_3d,
        "three_d_reconstruction_status": "suitable" if suitable_for_3d else "unsuitable",
        "low_parallax_or_rotation_suspected": low_parallax_suspected,
        "suitability_reasons": suitability_reasons,
        "next_step_recommendation": (
            "continue_alignment"
            if suitable_for_3d
            else (
                "reshoot_with_translation"
                if low_parallax_suspected
                else "retry_sfm_with_forward_flight_settings"
            )
        ),
        "mean_reprojection_error": None if mean_reprojection_error is None else float(mean_reprojection_error),
        "used_mask": bool(config.use_mask),
        "frame_step": int(config.frame_step),
        "start_frame": int(config.start_frame),
        "num_frames": int(config.num_frames),
        "ba_global_frames_ratio": float(config.ba_global_frames_ratio),
        "ba_global_points_ratio": float(config.ba_global_points_ratio),
        "ba_global_frames_freq": int(config.ba_global_frames_freq),
        "ba_global_points_freq": int(config.ba_global_points_freq),
        "ba_global_max_num_iterations": int(config.ba_global_max_num_iterations),
        "ba_global_max_refinements": int(config.ba_global_max_refinements),
        "backend": backend,
        "requested_device": runtime_values.get("requested_device", config.device),
        "effective_device": runtime_values.get("effective_device"),
        "gpu_index": runtime_values.get("gpu_index", config.gpu_index),
        "gpu_name": runtime_values.get("gpu_name"),
        "feature_extraction_gpu": bool(runtime_values.get("feature_extraction_gpu", False)),
        "feature_matching_gpu": bool(runtime_values.get("feature_matching_gpu", False)),
        "bundle_adjustment_gpu": bool(runtime_values.get("bundle_adjustment_gpu", False)),
        "cpu_fallback": bool(runtime_values.get("cpu_fallback", False)),
        "colmap_version": runtime_values.get("colmap_version"),
        "pycolmap_version": runtime_values.get("pycolmap_version"),
        "elapsed_sec": runtime_values.get("elapsed_sec"),
        "stage_timings": runtime_values.get("stage_timings", {}),
        "camera_model": config.camera_model,
        "max_image_size": int(config.max_image_size),
        "max_num_features": int(config.max_num_features),
        "warnings": warning_items,
    }


def build_sfm_report(
    *,
    video_path: str | Path | None,
    output_dir: str | Path,
    stats: Mapping[str, Any],
    outputs: Mapping[str, Any],
) -> str:
    reprojection = stats.get("mean_reprojection_error")
    reprojection_text = "不可用" if reprojection is None else f"{float(reprojection):.4f} px"
    warnings = stats.get("warnings") or []
    lines = [
        "# SfM 重建报告",
        "",
        f"- 输入视频：{video_path or 'export-only/mock'}",
        f"- 输出目录：{output_dir}",
        f"- 抽帧范围：start={stats.get('start_frame')}，num_frames={stats.get('num_frames')}",
        f"- frame_step：{stats.get('frame_step')}",
        f"- 是否使用 mask：{'是' if stats.get('used_mask') else '否'}",
        f"- 使用后端：{stats.get('backend')}",
        f"- 请求设备：{stats.get('requested_device')}",
        f"- 实际设备：{stats.get('effective_device') or '未记录'}",
        f"- GPU：{stats.get('gpu_name') or '未使用或未验证'}",
        f"- 注册帧数：{stats.get('registered_count')} / {stats.get('extracted_frame_count')}",
        f"- 点云数量：{stats.get('point_count')}",
        f"- 三维重建适用性：{'适合' if stats.get('suitable_for_3d', True) else '不适合三维重建'}",
        f"- 平均重投影误差：{reprojection_text}",
        "",
        "## 输出文件",
        "",
        *(f"- {name}：{path}" for name, path in outputs.items()),
        "",
        "## Warning",
        "",
        *(f"- {item}" for item in (warnings or ["无"])),
        *(
            [
                "",
                "## 重新拍摄建议",
                "",
                "- 当前结果不应将点云用于 CAD 配准或道路表面诊断。",
                "- 请让无人机产生明显平移，例如沿路线飞行或围绕场地移动；仅原地旋转无法提供可靠三角测量视差。",
            ]
            if stats.get("next_step_recommendation") == "reshoot_with_translation"
            else []
        ),
        *(
            [
                "",
                "## SfM 注册失败处理",
                "",
                "- 当前结果只注册了少量相机帧，不能用于后续 CAD 配准。",
                "- 点云数量较多并不表示相机轨迹完整，不能据此判断视频缺少平移。",
                "- 请使用前向飞行参数重新运行 SfM，并检查顺序匹配与相机内参。",
            ]
            if stats.get("next_step_recommendation") == "retry_sfm_with_forward_flight_settings"
            else []
        ),
        "",
        "## 当前限制",
        "",
        "- DINOv3 segmentation 尚未迁移。",
        "- 默认无语义 mask。",
        "- SfM 结果需通过 alignment 与 CAD 坐标系对齐。",
        "",
    ]
    return "\n".join(lines)


def write_sparse_ply(path: str | Path, points: np.ndarray, colors: np.ndarray | None = None) -> Path:
    xyz = np.asarray(points, dtype=np.float64).reshape((-1, 3))
    rgb = None if colors is None else np.clip(np.asarray(colors), 0, 255).astype(np.uint8).reshape((-1, 3))
    if rgb is not None and len(rgb) != len(xyz):
        raise ValueError("point/color count mismatch")
    header = [
        "ply",
        "format ascii 1.0",
        f"element vertex {len(xyz)}",
        "property float x",
        "property float y",
        "property float z",
    ]
    if rgb is not None:
        header.extend(["property uchar red", "property uchar green", "property uchar blue"])
    header.append("end_header")
    rows = []
    for index, point in enumerate(xyz):
        line = f"{point[0]:.9g} {point[1]:.9g} {point[2]:.9g}"
        if rgb is not None:
            line += f" {int(rgb[index, 0])} {int(rgb[index, 1])} {int(rgb[index, 2])}"
        rows.append(line)
    return write_text(path, "\n".join([*header, *rows]) + "\n")


def _value(value):
    return value() if callable(value) else value


def _export_from_reconstruction(
    reconstruction,
    *,
    frame_indices: Sequence[int],
    fps: float,
    width: int,
    height: int,
    video_path: str | Path,
) -> tuple[dict, list[dict], np.ndarray, np.ndarray | None, float | None]:
    images = {}
    for image_id in reconstruction.reg_image_ids():
        image = reconstruction.image(image_id)
        images[str(image.name)] = image
    poses: list[dict] = []
    for frame_index in frame_indices:
        image = images.get(frame_image_name(frame_index))
        if image is None or not bool(_value(image.has_pose)):
            poses.append({"frame_index": int(frame_index), "registered": False})
            continue
        transform = _value(image.cam_from_world)
        rotation = _value(transform.rotation)
        quat_xyzw = np.asarray(_value(rotation.quat), dtype=np.float64)
        center = np.asarray(_value(image.projection_center), dtype=np.float64)
        pose = {
            "frame_index": int(frame_index),
            "registered": True,
            "center": center.tolist(),
            "cam_from_world_quat_wxyz": [
                float(quat_xyzw[3]),
                float(quat_xyzw[0]),
                float(quat_xyzw[1]),
                float(quat_xyzw[2]),
            ],
        }
        observations = getattr(image, "num_points3D", None)
        if observations is not None:
            pose["num_observations"] = int(_value(observations))
        poses.append(pose)
    intrinsics: list[dict] = []
    for camera_id in reconstruction.cameras:
        camera = reconstruction.camera(camera_id)
        intrinsics.append(
            {
                "camera_id": int(camera_id),
                "model": str(_value(camera.model_name)),
                "width": int(_value(camera.width)),
                "height": int(_value(camera.height)),
                "params": [float(value) for value in _value(camera.params)],
            }
        )
    point_rows = []
    color_rows = []
    for point_id in reconstruction.points3D:
        point = reconstruction.point3D(point_id)
        point_rows.append(np.asarray(_value(point.xyz), dtype=np.float64))
        color = getattr(point, "color", None)
        if color is not None:
            color_rows.append(np.asarray(_value(color), dtype=np.uint8))
    points = np.asarray(point_rows, dtype=np.float64).reshape((-1, 3))
    colors = np.asarray(color_rows, dtype=np.uint8).reshape((-1, 3)) if len(color_rows) == len(point_rows) and point_rows else None
    mean_error = None
    compute_error = getattr(reconstruction, "compute_mean_reprojection_error", None)
    if compute_error is not None:
        mean_error = float(_value(compute_error))
    trajectory = build_camera_trajectory(
        fps=fps,
        width=width,
        height=height,
        intrinsics=intrinsics,
        poses=poses,
        video_path=video_path,
    )
    return trajectory, intrinsics, points, colors, mean_error


def _video_metadata(video_path: Path) -> tuple[int, float, int, int]:
    cv2 = importlib.import_module("cv2")
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    try:
        return (
            int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
            float(capture.get(cv2.CAP_PROP_FPS) or 25.0),
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        )
    finally:
        capture.release()


def _run_colmap_features(config: ReconstructionConfig, images_dir: Path, masks_dir: Path, database_path: Path) -> None:
    if not config.colmap_exe:
        return
    command = [
        config.colmap_exe,
        "feature_extractor",
        "--database_path",
        str(database_path),
        "--image_path",
        str(images_dir),
        "--ImageReader.single_camera",
        "1",
        "--ImageReader.camera_model",
        config.camera_model,
        "--SiftExtraction.max_image_size",
        str(config.max_image_size),
        "--SiftExtraction.max_num_features",
        str(config.max_num_features),
    ]
    if config.use_mask:
        command.extend(["--ImageReader.mask_path", str(masks_dir)])
    subprocess.run(command, check=True, **hidden_process_options())
    subprocess.run(
        [
            config.colmap_exe,
            "sequential_matcher",
            "--database_path",
            str(database_path),
            "--SequentialMatching.overlap",
            str(config.sequential_overlap),
        ],
        check=True,
        **hidden_process_options(),
    )


def run_reconstruction(
    *,
    video_path: str | Path,
    output_dir: str | Path,
    config: ReconstructionConfig,
    seg_dir: str | Path | None = None,
    progress_callback: Callable[[str, float, str], None] | None = None,
) -> ReconstructionResult:
    started_at = time.perf_counter()
    runtime_label = ""

    def notify(substage: str, progress: float, message: str) -> None:
        print(f"[sfm:{substage}] {message}", flush=True)
        if progress_callback:
            status_message = f"{message}（{runtime_label}）" if runtime_label else message
            progress_callback(substage, progress, status_message)

    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"video does not exist: {video}")
    environment = detect_sfm_environment(config.colmap_exe, requested_device=config.device)
    selection = select_sfm_backend(
        environment,
        backend=config.backend,
        device=config.device,
        gpu_index=config.gpu_index,
        allow_cpu_fallback=not config.no_cpu_fallback,
    )
    runtime_label = f"后端={selection.backend}，设备={selection.effective_device}"
    warnings: list[str] = list(selection.warnings)
    notify(
        "backend",
        0.05,
        f"后端={selection.backend}，设备={selection.effective_device}",
    )
    output = ensure_dir(output_dir)
    images_dir = ensure_dir(output / "images")
    masks_dir = ensure_dir(output / "masks")
    sparse_dir = ensure_dir(output / "sparse")
    database_path = output / "database.db"
    frame_count, fps, width, height = _video_metadata(video)
    frame_indices = frame_indices_for_config(config, frame_count)
    if len(frame_indices) < 3:
        raise RuntimeError("SfM requires at least 3 extracted frames")
    pycolmap = None
    if config.export_only:
        try:
            pycolmap = load_pycolmap()
        except RuntimeError:
            pycolmap = None
        if pycolmap is not None:
            reconstruction = load_best_sparse_model(pycolmap, sparse_dir)
            trajectory, intrinsics, points, colors, mean_error = _export_from_reconstruction(
                reconstruction,
                frame_indices=frame_indices,
                fps=fps,
                width=width,
                height=height,
                video_path=video,
            )
        else:
            executable = environment.get("colmap_path")
            if not executable:
                raise RuntimeError("export-only requires pycolmap or an official COLMAP CLI")
            text_dir = convert_colmap_model_to_text(
                executable,
                first_sparse_model_dir(sparse_dir),
                output / "sparse_text_export",
            )
            exported = export_colmap_text_model(
                text_dir,
                frame_indices=frame_indices,
                fps=fps,
                width=width,
                height=height,
                video_path=video,
            )
            trajectory, intrinsics, points, colors, mean_error = (
                exported.trajectory,
                exported.intrinsics,
                exported.points,
                exported.colors,
                exported.mean_reprojection_error,
            )
        registered_count = sum(bool(pose.get("registered")) for pose in trajectory["poses"])
        runtime = {
            **selection.to_dict(),
            "colmap_version": environment.get("colmap_version"),
            "pycolmap_version": environment.get("pycolmap_version"),
            "elapsed_sec": time.perf_counter() - started_at,
        }
        stats = build_sfm_stats(
            frame_count=frame_count,
            extracted_frame_count=len(frame_indices),
            registered_count=registered_count,
            point_count=len(points),
            mean_reprojection_error=mean_error,
            config=config,
            backend=f"{selection.backend}_export_only",
            warnings=warnings,
            runtime=runtime,
        )
        return ReconstructionResult(
            trajectory=trajectory,
            intrinsics=intrinsics,
            points=points,
            colors=colors,
            stats=stats,
            warnings=warnings,
            frame_timestamps=[],
        )
    notify("extract_frames", 0.12, "正在从视频抽帧")
    frames = extract_frames(video, images_dir, frame_indices)
    if config.use_mask:
        if seg_dir is None:
            raise FileNotFoundError("--use-mask requires --seg-dir")
        _, mask_warnings = prepare_masks(seg_dir, masks_dir, frames)
        warnings.extend(mask_warnings)
    if not config.reuse_database and database_path.exists():
        database_path.unlink()
    runtime: dict[str, Any] = {
        **selection.to_dict(),
        "colmap_version": environment.get("colmap_version"),
        "pycolmap_version": environment.get("pycolmap_version"),
        "feature_extraction_gpu": False,
        "feature_matching_gpu": False,
        "bundle_adjustment_gpu": False,
        "stage_timings": {},
    }
    if selection.backend == "colmap_cli":
        executable = environment.get("colmap_path")
        if not executable:
            raise RuntimeError("COLMAP CLI not found; provide --colmap-exe or set COLMAP_EXE")
        cli_paths = ColmapCliPaths(
            images_dir=images_dir,
            masks_dir=masks_dir,
            database_path=database_path,
            sparse_dir=sparse_dir,
        )
        cli_kwargs = {
            "camera_model": config.camera_model,
            "max_image_size": config.max_image_size,
            "max_num_features": config.max_num_features,
            "sequential_overlap": config.sequential_overlap,
            "init_min_tri_angle": config.init_min_tri_angle,
            "ba_global_frames_ratio": config.ba_global_frames_ratio,
            "ba_global_points_ratio": config.ba_global_points_ratio,
            "ba_global_frames_freq": config.ba_global_frames_freq,
            "ba_global_points_freq": config.ba_global_points_freq,
            "ba_global_max_num_iterations": config.ba_global_max_num_iterations,
            "ba_global_max_refinements": config.ba_global_max_refinements,
            "use_mask": config.use_mask,
            "gpu_index": config.gpu_index,
            "progress_callback": notify,
        }
        try:
            cli_result = run_colmap_cli_pipeline(
                executable,
                cli_paths,
                use_gpu=selection.effective_device == "cuda",
                **cli_kwargs,
            )
        except ColmapCommandError as exc:
            can_retry_cpu = (
                selection.effective_device == "cuda"
                and not config.no_cpu_fallback
                and is_cuda_failure(str(exc))
            )
            if not can_retry_cpu:
                raise
            warnings.append("COLMAP CUDA unavailable at runtime; retrying feature extraction and matching on CPU")
            if database_path.exists():
                database_path.unlink()
            shutil.rmtree(sparse_dir, ignore_errors=True)
            ensure_dir(sparse_dir)
            runtime["effective_device"] = "cpu"
            runtime["cpu_fallback"] = True
            cli_result = run_colmap_cli_pipeline(
                executable,
                cli_paths,
                use_gpu=False,
                **cli_kwargs,
            )
        runtime["stage_timings"] = cli_result["timings"]
        runtime["feature_extraction_gpu"] = bool(cli_result["feature_extraction_gpu"])
        runtime["feature_matching_gpu"] = bool(cli_result["feature_matching_gpu"])
        gpu_verified = runtime["feature_extraction_gpu"] and runtime["feature_matching_gpu"]
        if selection.effective_device == "cuda" and not gpu_verified:
            warning = "COLMAP CUDA execution could not be verified from command logs; recorded as CPU fallback"
            if config.no_cpu_fallback:
                raise RuntimeError(warning)
            warnings.append(warning)
            runtime["effective_device"] = "cpu"
            runtime["cpu_fallback"] = True
        try:
            pycolmap = load_pycolmap()
        except RuntimeError:
            pycolmap = None
        if pycolmap is not None:
            reconstruction = load_best_sparse_model(pycolmap, sparse_dir)
            trajectory, intrinsics, points, colors, mean_error = _export_from_reconstruction(
                reconstruction,
                frame_indices=[frame.frame_index for frame in frames],
                fps=fps,
                width=width,
                height=height,
                video_path=video,
            )
        else:
            text_dir = convert_colmap_model_to_text(
                executable,
                first_sparse_model_dir(sparse_dir),
                output / "sparse_text_export",
            )
            exported = export_colmap_text_model(
                text_dir,
                frame_indices=[frame.frame_index for frame in frames],
                fps=fps,
                width=width,
                height=height,
                video_path=video,
            )
            trajectory, intrinsics, points, colors, mean_error = (
                exported.trajectory,
                exported.intrinsics,
                exported.points,
                exported.colors,
                exported.mean_reprojection_error,
            )
    else:
        pycolmap = load_pycolmap()
        use_gpu = selection.effective_device == "cuda"
        extraction_options, matching_options, pycolmap_device = configure_pycolmap_feature_options(
            pycolmap,
            use_gpu=use_gpu,
            gpu_index=config.gpu_index,
        )
        reader_options = pycolmap.ImageReaderOptions()
        reader_options.camera_model = config.camera_model
        if config.use_mask:
            reader_options.mask_path = str(masks_dir)
        try:
            extraction_options.max_image_size = config.max_image_size
            extraction_options.sift.max_num_features = config.max_num_features
        except Exception:
            warnings.append("当前 pycolmap 版本未接受全部 feature 参数，已使用兼容默认值")
        if not config.reuse_database or not database_path.exists():
            notify("feature_extraction", 0.32, "正在提取特征")
            feature_started = time.perf_counter()
            pycolmap.extract_features(
                str(database_path),
                str(images_dir),
                camera_mode=pycolmap.CameraMode.SINGLE,
                reader_options=reader_options,
                extraction_options=extraction_options,
                device=pycolmap_device,
            )
            runtime["stage_timings"]["feature_extraction_sec"] = time.perf_counter() - feature_started
            pairing = pycolmap.SequentialPairingOptions()
            pairing.overlap = config.sequential_overlap
            pairing.quadratic_overlap = config.quadratic_overlap
            notify("feature_matching", 0.52, "正在进行顺序匹配")
            matching_started = time.perf_counter()
            pycolmap.match_sequential(
                str(database_path),
                matching_options=matching_options,
                pairing_options=pairing,
                device=pycolmap_device,
            )
            runtime["stage_timings"]["feature_matching_sec"] = time.perf_counter() - matching_started
        options = pycolmap.IncrementalPipelineOptions()
        try:
            configure_incremental_mapping_options(options, config)
        except Exception:
            warnings.append("当前 pycolmap 版本未接受完整前向飞行参数，已使用兼容默认值")
        notify("mapper", 0.72, "正在稀疏重建")
        mapper_started = time.perf_counter()
        reconstructions = pycolmap.incremental_mapping(
            str(database_path),
            str(images_dir),
            str(sparse_dir),
            options=options,
        )
        runtime["stage_timings"]["mapper_sec"] = time.perf_counter() - mapper_started
        if not reconstructions:
            raise RuntimeError("COLMAP reconstruction failed: no sparse model was produced")
        reconstruction = max(reconstructions.values(), key=lambda item: item.num_reg_images())
        trajectory, intrinsics, points, colors, mean_error = _export_from_reconstruction(
            reconstruction,
            frame_indices=[frame.frame_index for frame in frames],
            fps=fps,
            width=width,
            height=height,
            video_path=video,
        )
        runtime["feature_extraction_gpu"] = use_gpu
        runtime["feature_matching_gpu"] = use_gpu
    registered_count = sum(bool(pose.get("registered")) for pose in trajectory["poses"])
    if registered_count < config.min_reg_images:
        warnings.append(f"注册帧数 {registered_count} 低于 min_reg_images={config.min_reg_images}")
    runtime["elapsed_sec"] = time.perf_counter() - started_at
    notify("export", 0.92, "正在导出统一 SfM 产物")
    stats = build_sfm_stats(
        frame_count=frame_count,
        extracted_frame_count=len(frames),
        registered_count=registered_count,
        point_count=len(points),
        mean_reprojection_error=mean_error,
        config=config,
        backend=selection.backend,
        warnings=warnings,
        runtime=runtime,
    )
    return ReconstructionResult(
        trajectory=trajectory,
        intrinsics=intrinsics,
        points=points,
        colors=colors,
        stats=stats,
        warnings=warnings,
        frame_timestamps=frame_timestamp_rows(frames),
    )


def export_mock_reconstruction(path: str | Path, config: ReconstructionConfig) -> ReconstructionResult:
    data = read_json(path)
    poses = list(data.get("poses") or [])
    intrinsics = list(data.get("intrinsics") or [])
    points_raw = np.asarray(data.get("points") or [], dtype=np.float64)
    points = points_raw[:, :3] if points_raw.size else np.empty((0, 3), dtype=np.float64)
    colors = np.clip(points_raw[:, 3:6], 0, 255).astype(np.uint8) if points_raw.ndim == 2 and points_raw.shape[1] >= 6 else None
    trajectory = build_camera_trajectory(
        fps=float(data.get("fps", 25.0)),
        width=int(data.get("width", 0)),
        height=int(data.get("height", 0)),
        intrinsics=intrinsics,
        poses=poses,
    )
    registered_count = sum(bool(pose.get("registered", True)) for pose in poses)
    stats = build_sfm_stats(
        frame_count=len(poses),
        extracted_frame_count=len(poses),
        registered_count=registered_count,
        point_count=len(points),
        mean_reprojection_error=data.get("mean_reprojection_error"),
        config=config,
        backend="mock_export",
    )
    return ReconstructionResult(trajectory=trajectory, intrinsics=intrinsics, points=points, colors=colors, stats=stats)


def write_reconstruction_outputs(
    output_dir: str | Path,
    result: ReconstructionResult,
    *,
    video_path: str | Path | None,
) -> dict[str, Path]:
    output = ensure_dir(output_dir)
    paths = {
        "camera_trajectory": output / "camera_trajectory.json",
        "sparse_points": output / "sparse_points.ply",
        "camera_intrinsics": output / "camera_intrinsics.json",
        "sfm_stats": output / "sfm_stats.json",
        "sfm_report": output / "sfm_report.md",
        "frame_timestamps": output / "frame_timestamps.csv",
    }
    write_json(paths["camera_trajectory"], result.trajectory)
    write_sparse_ply(paths["sparse_points"], result.points, result.colors)
    write_json(paths["camera_intrinsics"], {"intrinsics": result.intrinsics})
    write_json(paths["sfm_stats"], result.stats)
    write_csv_utf8_sig(
        paths["frame_timestamps"],
        result.frame_timestamps,
        fieldnames=(
            "source_frame_index",
            "extracted_index",
            "image_name",
            "pts_time_sec",
            "timestamp_source",
            "cfr_confirmed",
        ),
    )
    write_text(
        paths["sfm_report"],
        build_sfm_report(video_path=video_path, output_dir=output, stats=result.stats, outputs=paths),
    )
    return paths
