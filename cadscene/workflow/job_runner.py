from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from cadscene.alignment.keyframes import confirmed_keyframes
from cadscene.core.config import load_dataset_config
from cadscene.workflow.data_import import load_dataset_manifest
from cadscene.workflow.job_status import JobStatusStore
from cadscene.workflow.keyframe_plan import (
    keyframe_plan_path,
    keyframe_plan_progress_changed,
    load_keyframe_plan,
    sync_keyframe_plan,
    validate_quality_plan,
    write_keyframe_plan,
)


ALLOWED_STAGES = {"sfm", "alignment", "quality", "render", "pure_rotation"}


class JobAlreadyRunningError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.005 * (2**attempt), 0.05))
    finally:
        temporary.unlink(missing_ok=True)


def _background_process_options(*, new_process_group: bool = False) -> dict[str, int]:
    """返回 Windows 后台任务需要的进程标志，避免弹出控制台窗口。"""
    if os.name != "nt":
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return {"creationflags": flags}


def _child_process_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """固定 Python 子进程输出编码，保证工作流日志可按 UTF-8 读取。"""
    environment = dict(os.environ if base is None else base)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["CADSCENE_WORKFLOW_LOG_ENCODING"] = "utf-8"
    return environment


def read_workflow_log_text(path: str | Path) -> str:
    """读取新 UTF-8 日志，并兼容修复前由 Windows GBK 写出的日志。"""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _manual_keyframe_count(path: Path) -> int:
    """统计可作为 Sim3 对齐锚点的人工关键帧。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取人工关键帧文件: {path}") from error
    return len(confirmed_keyframes(payload)) if isinstance(payload, dict) else 0


def _safe_name(value: str, label: str) -> str:
    text = str(value)
    if not text or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in text):
        raise ValueError(f"invalid {label}")
    return text


def _local_path(root: Path, value: str | Path | None) -> Path | None:
    if value in (None, ""):
        return None
    text = str(value)
    if text.startswith(("http://", "https://")):
        raise ValueError("workflow runner requires a local path, not an HTTP URL")
    if text.startswith("/") and not text.startswith("//"):
        path = root / text.lstrip("/")
    else:
        path = Path(text)
    if not path.is_absolute():
        path = root / str(path).lstrip("/\\")
    return path.resolve()


def _first_existing(candidates: Sequence[Path | None], label: str) -> Path:
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate
    rendered = ", ".join(str(item) for item in candidates if item is not None)
    raise FileNotFoundError(f"{label} not found; checked: {rendered}")


def _python_has_pycolmap(python: str | Path) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-c", "import pycolmap"],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            env=_child_process_environment(),
            **_background_process_options(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _difusser_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("CADSCENE_SFM_PYTHON")
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.append(configured_path / "python.exe" if configured_path.is_dir() else configured_path)

    current = Path(sys.executable).resolve()
    candidates.append(current)
    prefixes = [current.parent]
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        prefixes.append(Path(conda_prefix).resolve())
    for prefix in prefixes:
        if prefix.parent.name.lower() == "envs":
            conda_root = prefix.parent.parent
        else:
            conda_root = prefix
        candidates.append(conda_root / "envs" / "difusser" / "python.exe")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def resolve_sfm_python() -> Path:
    """选择带 pycolmap 的解释器，优先尊重显式环境变量。"""
    for candidate in _difusser_python_candidates():
        if candidate.is_file() and _python_has_pycolmap(candidate):
            return candidate.resolve()
    return Path(sys.executable).resolve()


def _last_log_line(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        lines = read_workflow_log_text(path).splitlines()
    except OSError:
        return None
    return next((line.strip() for line in reversed(lines) if line.strip()), None)


def _command_option(command: Sequence[str], flag: str) -> str | None:
    values = [str(item) for item in command]
    try:
        index = values.index(flag)
    except ValueError:
        return None
    return values[index + 1] if index + 1 < len(values) else None


def _sfm_is_suitable_for_3d(stats_path: Path) -> bool | None:
    if not stats_path.exists():
        return None
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    explicit = stats.get("suitable_for_3d")
    if isinstance(explicit, bool):
        return explicit
    registered = int(stats.get("registered_count") or 0)
    extracted = int(stats.get("extracted_frame_count") or 0)
    points = int(stats.get("point_count") or 0)
    if not any((registered, extracted, points)):
        return None
    registered_ratio = registered / extracted if extracted else 0.0
    return registered >= 3 and registered_ratio >= 0.2 and points >= max(100, registered)


def _sfm_completion_message(stats: Mapping[str, Any]) -> str:
    if stats.get("suitable_for_3d") is not False:
        return "任务完成"
    if stats.get("low_parallax_or_rotation_suspected") is True:
        return (
            "SfM 已完成，但当前结果不适合三维重建；"
            "请重新拍摄并让无人机产生明显平移，例如沿路线飞行或围绕场地移动"
        )
    registered = int(stats.get("registered_count") or 0)
    extracted = int(stats.get("extracted_frame_count") or 0)
    return (
        f"SfM 注册失败（{registered}/{extracted} 帧），不能据此判断视频缺少平移；"
        "请使用前向飞行参数重新运行 SfM，并检查匹配和相机内参"
    )


def _require_suitable_sfm(run_dir: Path) -> None:
    stats_path = run_dir / "02_sfm" / "sfm_stats.json"
    suitable = _sfm_is_suitable_for_3d(stats_path)
    if suitable is False:
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            stats = {"suitable_for_3d": False}
        raise ValueError(_sfm_completion_message(stats))


def resolve_stage_inputs(
    root: str | Path,
    dataset: str,
    run_id: str,
    options: Mapping[str, Any] | None = None,
    *,
    require_alignment_inputs: bool = True,
) -> dict[str, Any]:
    base = Path(root).resolve()
    dataset_name = _safe_name(dataset, "dataset")
    run_name = _safe_name(run_id, "runId")
    opts = dict(options or {})
    config_path = base / "configs" / "datasets" / f"{dataset_name}.yaml"
    dataset_config = load_dataset_config(config_path) if config_path.exists() else {"dataset_name": dataset_name}
    try:
        dataset_manifest = load_dataset_manifest(base, dataset_name)
    except FileNotFoundError:
        dataset_manifest = {}
    manifest_video = (dataset_manifest.get("video") or {}).get("path")
    manifest_cad = (dataset_manifest.get("cad") or {}).get("design_json")
    manifest_defaults = dataset_manifest.get("defaults") or {}
    workflow = str(
        (dataset_manifest.get("workflow") or {}).get("trajectory_mode") or "sfm_only"
    )
    full_pose = workflow == "srt_full_pose"
    fixed_track = workflow == "srt_fixed_track_visual_pose"
    no_sparse_geometry = full_pose or fixed_track

    video_option = opts.get("video") or opts.get("video_path")
    video = _first_existing(
        [
            _local_path(base, video_option),
            _local_path(base, manifest_video),
            _local_path(base, dataset_config.get("video_path")),
            base / "data" / dataset_name / f"{dataset_name}.mp4",
            base / "data" / dataset_name / "hygs_1min.mp4",
            base / "data" / dataset_name / "video" / f"{dataset_name}.mp4",
            base / "data" / dataset_name / "video" / "hygs_1min.mp4",
        ],
        "video",
    )
    cad_dir = None
    cad_scale = None
    origin_xy = None
    if require_alignment_inputs:
        cad_option = opts.get("cad_dir") or opts.get("cad")
        cad_candidate = (
            _local_path(base, cad_option)
            or _local_path(base, manifest_cad)
            or _local_path(base, dataset_config.get("cad_dir"))
        )
        if cad_candidate is not None and cad_candidate.is_file():
            cad_candidate = cad_candidate.parent
        cad_dir = _first_existing(
            [cad_candidate, base / "data" / dataset_name / "cad", base / "data" / dataset_name],
            "cad_dir",
        )
        cad_scale_value = opts.get("cad_scale")
        if cad_scale_value is None:
            cad_scale_value = manifest_defaults.get("cad_scale")
        if cad_scale_value is None:
            cad_scale_value = dataset_config.get("cad_scale", 0.06 if dataset_name == "hygs_1min" else 0.0)
        cad_scale = float(cad_scale_value)
        origin_xy = opts.get("origin_xy")
        if origin_xy is None:
            origin_xy = manifest_defaults.get("origin_xy")
        if origin_xy is None:
            origin_xy = dataset_config.get("origin_xy")
        if origin_xy is None and dataset_name == "hygs_1min":
            origin_xy = [567747.5756295, 3330464.2234675]
        if cad_scale <= 0 or not isinstance(origin_xy, (list, tuple)) or len(origin_xy) != 2:
            raise ValueError("cad_scale and origin_xy are required")
    run_dir = base / "runs" / dataset_name / run_name
    return {
        "root": base,
        "dataset": dataset_name,
        "run_id": run_name,
        "run_dir": run_dir,
        "video": video,
        "cad_dir": cad_dir,
        "cad_scale": cad_scale,
        "origin_xy": [float(origin_xy[0]), float(origin_xy[1])] if origin_xy is not None else None,
        "workflow": workflow,
        "trajectory": (
            run_dir / "02_srt_full_pose" / "camera_trajectory_full_pose.json"
            if full_pose
            else run_dir
            / "02_srt_visual_pose"
            / "camera_trajectory_visual_pose.json"
            if fixed_track
            else run_dir / "02_sfm" / "camera_trajectory.json"
        ),
        "sparse_ply": (
            None
            if no_sparse_geometry
            else run_dir / "02_sfm" / "sparse_points.ply"
        ),
        "manual_track": run_dir / "01_keyframes" / "camera_track_manual.json",
        "sfm_camera_path": run_dir / "03_alignment" / "sfm_camera_path.csv",
        "render_output": run_dir / "08_render" / "sfm_align_overlay.mp4",
    }


def _build_stage_command_legacy(
    root: str | Path,
    dataset: str,
    run_id: str,
    stage: str,
    options: Mapping[str, Any] | None = None,
) -> list[str]:
    if stage not in ALLOWED_STAGES:
        raise ValueError(f"unsupported workflow stage: {stage}")
    resolved = resolve_stage_inputs(
        root,
        dataset,
        run_id,
        options,
        require_alignment_inputs=stage != "sfm",
    )
    python = str(resolve_sfm_python()) if stage == "sfm" else sys.executable
    common = ["--dataset", resolved["dataset"], "--run-id", resolved["run_id"], "--output-root", str(resolved["root"] / "runs")]
    if stage == "sfm":
        return [
            python,
            "-m",
            "cadscene.cli.run_sfm",
            *common,
            "--video",
            str(resolved["video"]),
            "--start-frame",
            "0",
            "--frame-step",
            "5",
            "--init-min-tri-angle",
            "2",
            "--no-mask",
        ]
    if stage == "quality":
        for label in ("trajectory", "sparse_ply", "manual_track"):
            if not resolved[label].exists():
                if label in {"trajectory", "sparse_ply"}:
                    raise FileNotFoundError("请先完成 SfM 重建，或选择已有 SfM 结果。")
                raise FileNotFoundError(f"manual camera track not found: {resolved[label]}")
        return [
            python,
            "-m",
            "cadscene.cli.run_pipeline",
            *common,
            "--config",
            str(resolved["root"] / "configs" / "pipelines" / "sfm_overlay_existing_sfm.yaml"),
            "--trajectory",
            str(resolved["trajectory"]),
            "--sparse-ply",
            str(resolved["sparse_ply"]),
            "--web-camera-track",
            str(resolved["manual_track"]),
            "--video",
            str(resolved["video"]),
            "--cad-dir",
            str(resolved["cad_dir"]),
            "--cad-scale",
            str(resolved["cad_scale"]),
            "--origin-xy",
            str(resolved["origin_xy"][0]),
            str(resolved["origin_xy"][1]),
            "--skip-render",
        ]
    required = ("trajectory", "sparse_ply", "manual_track")
    for label in required:
        if not resolved[label].exists():
            if label in {"trajectory", "sparse_ply"}:
                raise FileNotFoundError("请先完成 SfM 重建，或选择已有 SfM 结果。")
            raise FileNotFoundError(f"manual camera track not found: {resolved[label]}")
    _require_suitable_sfm(resolved["run_dir"])
    anchor_count = _manual_keyframe_count(resolved["manual_track"])
    if anchor_count < 2:
        raise ValueError(f"渲染前路线拟合至少需要 2 个人工关键帧；当前为 {anchor_count} 个。")
    return [
        python,
        "-m",
        "cadscene.cli.run_pipeline",
        *common,
        "--config",
        str(resolved["root"] / "configs" / "pipelines" / "sfm_overlay_existing_sfm.yaml"),
        "--stages",
        "alignment,render",
        "--trajectory",
        str(resolved["trajectory"]),
        "--sparse-ply",
        str(resolved["sparse_ply"]),
        "--web-camera-track",
        str(resolved["manual_track"]),
        "--video",
        str(resolved["video"]),
        "--cad-dir",
        str(resolved["cad_dir"]),
        "--cad-scale",
        str(resolved["cad_scale"]),
        "--origin-xy",
        str(resolved["origin_xy"][0]),
        str(resolved["origin_xy"][1]),
    ]


def build_stage_command(
    root: str | Path,
    dataset: str,
    run_id: str,
    stage: str,
    options: Mapping[str, Any] | None = None,
    *,
    application_root: str | Path | None = None,
) -> list[str]:
    if stage not in ALLOWED_STAGES:
        raise ValueError(f"unsupported workflow stage: {stage}")
    resolved = resolve_stage_inputs(
        root,
        dataset,
        run_id,
        options,
        require_alignment_inputs=stage not in {"sfm", "pure_rotation"},
    )
    config_root = (
        Path(application_root).resolve()
        if application_root is not None
        else resolved["root"]
    )
    if stage == "pure_rotation":
        opts = dict(options or {})
        command = [
            sys.executable, "-m", "cadscene.cli.run_pure_rotation",
            "--dataset", resolved["dataset"], "--run-id", resolved["run_id"],
            "--output-root", str(resolved["root"] / "runs"), "--video", str(resolved["video"]),
        ]
        if opts.get("backend_root"):
            command.extend(["--backend-root", str(opts["backend_root"])])
        if opts.get("backend_command"):
            command.extend(["--backend-command", str(opts["backend_command"])])
        if opts.get("cadscene_readonly"):
            command.extend(["--cadscene-readonly", str(opts["cadscene_readonly"])])
        if opts.get("force"):
            command.append("--force")
        return command
    try:
        dataset_manifest = load_dataset_manifest(resolved["root"], resolved["dataset"])
    except FileNotFoundError:
        dataset_manifest = {}
    if stage == "render" and (dataset_manifest.get("workflow") or {}).get("trajectory_mode") == "pure_rotation":
        corrected = resolved["run_dir"] / "04_pure_rotation_corrections" / "camera_track_corrected.json"
        base = resolved["run_dir"] / "03_pure_rotation_placement" / "camera_track_cad_base.json"
        track = corrected if corrected.exists() else base
        if not track.exists():
            raise FileNotFoundError("请先完成 Pure-Rotation 全局放置和姿态关键帧拟合。")
        return [
            sys.executable,
            "-m",
            "cadscene.cli.render_pure_rotation",
            "--dataset",
            resolved["dataset"],
            "--run-id",
            resolved["run_id"],
            "--output-root",
            str(resolved["root"] / "runs"),
            "--video",
            str(resolved["video"]),
            "--cad-dir",
            str(resolved["cad_dir"]),
            "--cad-scale",
            str(resolved["cad_scale"]),
            "--origin-xy",
            str(resolved["origin_xy"][0]),
            str(resolved["origin_xy"][1]),
            "--track",
            str(track),
            "--max-distance-m",
            "900",
        ]
    python = str(resolve_sfm_python()) if stage == "sfm" else sys.executable
    common = ["--dataset", resolved["dataset"], "--run-id", resolved["run_id"], "--output-root", str(resolved["root"] / "runs")]
    if stage == "sfm":
        opts = dict(options or {})
        backend = str(opts.get("backend", "pycolmap"))
        device = str(opts.get("device", "cpu"))
        gpu_index = str(opts.get("gpu_index", "0"))
        ba_global_frames_ratio = float(opts.get("ba_global_frames_ratio", 2.0))
        ba_global_points_ratio = float(opts.get("ba_global_points_ratio", 2.0))
        ba_global_frames_freq = int(opts.get("ba_global_frames_freq", 1000))
        ba_global_points_freq = int(opts.get("ba_global_points_freq", 1_000_000))
        ba_global_max_num_iterations = int(opts.get("ba_global_max_num_iterations", 25))
        ba_global_max_refinements = int(opts.get("ba_global_max_refinements", 2))
        if backend not in {"auto", "pycolmap", "colmap_cli"}:
            raise ValueError("invalid SfM backend")
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError("invalid SfM device")
        if not gpu_index.lstrip("-").isdigit():
            raise ValueError("invalid SfM gpu_index")
        command = [
            python,
            "-m",
            "cadscene.cli.run_sfm",
            *common,
            "--video",
            str(resolved["video"]),
            "--start-frame",
            "0",
            "--frame-step",
            "5",
            "--init-min-tri-angle",
            "2",
            "--ba-global-frames-ratio",
            str(ba_global_frames_ratio),
            "--ba-global-points-ratio",
            str(ba_global_points_ratio),
            "--ba-global-frames-freq",
            str(ba_global_frames_freq),
            "--ba-global-points-freq",
            str(ba_global_points_freq),
            "--ba-global-max-num-iterations",
            str(ba_global_max_num_iterations),
            "--ba-global-max-refinements",
            str(ba_global_max_refinements),
            "--no-mask",
            "--backend",
            backend,
            "--device",
            device,
            "--gpu-index",
            gpu_index,
        ]
        if opts.get("colmap_exe"):
            command.extend(["--colmap-exe", str(opts["colmap_exe"])])
        if bool(opts.get("no_cpu_fallback")):
            command.append("--no-cpu-fallback")
        return command
    full_pose = resolved["workflow"] == "srt_full_pose"
    fixed_track = resolved["workflow"] == "srt_fixed_track_visual_pose"
    no_sparse_geometry = full_pose or fixed_track
    pipeline_config = (
        "srt_fixed_track_visual_pose_overlay.yaml"
        if fixed_track
        else "srt_full_pose_overlay.yaml"
        if full_pose
        else "sfm_overlay_existing_sfm.yaml"
    )
    if stage in {"alignment", "quality"}:
        if full_pose and stage == "quality":
            raise ValueError("full-pose workflow has no quality stage")
        if fixed_track and stage == "quality":
            raise ValueError("fixed-track visual pose workflow has no quality stage")
        required = ["trajectory", "manual_track"]
        if not no_sparse_geometry:
            required.append("sparse_ply")
        if stage == "quality":
            required.append("sfm_camera_path")
        for label in required:
            if not resolved[label].exists():
                if label in {"trajectory", "sparse_ply"}:
                    raise FileNotFoundError("请先完成 SfM 重建，或选择已有 SfM 结果。")
                if label == "sfm_camera_path":
                    raise FileNotFoundError("请先在关键帧标定阶段完成路线拟合。")
                raise FileNotFoundError(f"manual camera track not found: {resolved[label]}")
        if not no_sparse_geometry:
            _require_suitable_sfm(resolved["run_dir"])
        if stage == "alignment" and not no_sparse_geometry:
            anchor_count = _manual_keyframe_count(resolved["manual_track"])
            if anchor_count < 2:
                raise ValueError(f"路线拟合至少需要 2 个人工关键帧；当前为 {anchor_count} 个。")
        if stage == "quality":
            validate_quality_plan(keyframe_plan_path(resolved["run_dir"]), resolved["sfm_camera_path"])
        selected_stages = (
            "alignment"
            if fixed_track
            else "alignment,viewer_scene"
            if stage == "alignment"
            else "quality,viewer_scene"
            if full_pose
            else "quality,viewer_scene,road_surface"
        )
        command = [
            python,
            "-m",
            "cadscene.cli.run_pipeline",
            *common,
            "--config",
            str(config_root / "configs" / "pipelines" / pipeline_config),
            "--stages",
            selected_stages,
            "--trajectory",
            str(resolved["trajectory"]),
            "--web-camera-track",
            str(resolved["manual_track"]),
            "--video",
            str(resolved["video"]),
            "--cad-dir",
            str(resolved["cad_dir"]),
            "--cad-scale",
            str(resolved["cad_scale"]),
            "--origin-xy",
            str(resolved["origin_xy"][0]),
            str(resolved["origin_xy"][1]),
        ]
        if not no_sparse_geometry:
            trajectory_index = command.index("--trajectory")
            command[trajectory_index:trajectory_index] = [
                "--sparse-ply",
                str(resolved["sparse_ply"]),
            ]
        if stage == "quality":
            command.extend(
                [
                    "--progress-file",
                    str(resolved["run_dir"] / "logs/workflow/quality_progress.json"),
                ]
            )
        return command
    required = ["trajectory", "manual_track"]
    if not no_sparse_geometry:
        required.append("sparse_ply")
    for label in required:
        if not resolved[label].exists():
            if label in {"trajectory", "sparse_ply"}:
                raise FileNotFoundError("请先完成 SfM 重建，或选择已有 SfM 结果。")
            raise FileNotFoundError(f"manual camera track not found: {resolved[label]}")
    if not no_sparse_geometry:
        _require_suitable_sfm(resolved["run_dir"])
        anchor_count = _manual_keyframe_count(resolved["manual_track"])
        if anchor_count < 2:
            raise ValueError(f"渲染前路线拟合至少需要 2 个人工关键帧；当前为 {anchor_count} 个。")
    command = [
        python,
        "-m",
        "cadscene.cli.run_pipeline",
        *common,
        "--config",
        str(config_root / "configs" / "pipelines" / pipeline_config),
        "--stages",
        "alignment,render",
        "--trajectory",
        str(resolved["trajectory"]),
        "--web-camera-track",
        str(resolved["manual_track"]),
        "--video",
        str(resolved["video"]),
        "--cad-dir",
        str(resolved["cad_dir"]),
        "--cad-scale",
        str(resolved["cad_scale"]),
        "--origin-xy",
        str(resolved["origin_xy"][0]),
        str(resolved["origin_xy"][1]),
    ]
    if not no_sparse_geometry:
        trajectory_index = command.index("--trajectory")
        command[trajectory_index:trajectory_index] = [
            "--sparse-ply",
            str(resolved["sparse_ply"]),
        ]
    return command


class JobRunner:
    def __init__(
        self,
        root_dir: str | Path,
        *,
        application_root: str | Path | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.application_root = (
            Path(application_root).resolve()
            if application_root is not None
            else self.root_dir
        )
        self._lock = threading.RLock()
        self._processes: dict[tuple[str, str], subprocess.Popen] = {}
        self._cancelled: set[tuple[str, str]] = set()

    def _run_dir(self, dataset: str, run_id: str) -> Path:
        return self.root_dir / "runs" / _safe_name(dataset, "dataset") / _safe_name(run_id, "runId")

    def _process_path(self, dataset: str, run_id: str) -> Path:
        return self._run_dir(dataset, run_id) / "job_process.json"

    @staticmethod
    def _status_stage(stage: str) -> str:
        workflow_slots = {
            "alignment": "keyframes",
            "pure_rotation": "sfm",
        }
        return workflow_slots.get(stage, stage)

    def query(self, dataset: str, run_id: str) -> dict[str, Any] | None:
        path = self._process_path(dataset, run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))

    def start(self, dataset: str, run_id: str, stage: str, command: Sequence[str]) -> dict[str, Any]:
        if stage not in ALLOWED_STAGES:
            raise ValueError(f"unsupported workflow stage: {stage}")
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("command must be a non-empty list")
        key = (_safe_name(dataset, "dataset"), _safe_name(run_id, "runId"))
        with self._lock:
            active = self._processes.get(key)
            if active is not None and active.poll() is None:
                raise JobAlreadyRunningError("已有任务正在运行")
            existing = self.query(*key)
            if existing and existing.get("status") == "running" and self._pid_alive(int(existing.get("pid", 0))):
                raise JobAlreadyRunningError("已有任务正在运行")
            run_dir = self._run_dir(*key)
            log_file = run_dir / "logs" / "workflow" / f"{stage}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            progress_value = _command_option(command, "--progress-file")
            progress_file = Path(progress_value).resolve() if progress_value else None
            if progress_file is not None:
                try:
                    progress_file.relative_to(run_dir.resolve())
                except ValueError:
                    pass
                else:
                    progress_file.unlink(missing_ok=True)
            log_handle = log_file.open("wb", buffering=0)
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=str(Path(__file__).resolve().parents[2]),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=_child_process_environment(),
                **_background_process_options(new_process_group=True),
            )
            payload = {
                "dataset": key[0],
                "run_id": key[1],
                "stage": stage,
                "pid": process.pid,
                "status": "running",
                "command": [str(item) for item in command],
                "log_file": str(log_file),
                "started_at": _now_iso(),
                "ended_at": None,
                "returncode": None,
                "progress_file": None if progress_file is None else str(progress_file),
            }
            _atomic_json(self._process_path(*key), payload)
            JobStatusStore(run_dir / "job_status.json", run_id=key[1]).update_stage(
                self._status_stage(stage),
                status="running",
                progress=0.05,
                message="路线拟合任务已启动" if stage == "alignment" else "任务已启动",
                log_file=log_file,
                operation=stage,
            )
            self._processes[key] = process
            thread = threading.Thread(
                target=self._monitor,
                args=(key, stage, process, log_handle),
                daemon=True,
                name=f"cadscene-{key[0]}-{key[1]}-{stage}",
            )
            thread.start()
            return payload

    def start_stage(
        self,
        dataset: str,
        run_id: str,
        stage: str,
        options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = build_stage_command(
            self.root_dir,
            dataset,
            run_id,
            stage,
            options,
            application_root=self.application_root,
        )
        return self.start(dataset, run_id, stage, command)

    def _monitor(self, key: tuple[str, str], stage: str, process: subprocess.Popen, log_handle) -> None:
        progress_file = (self.query(*key) or {}).get("progress_file")
        while process.poll() is None:
            self._sync_progress(key, stage, progress_file)
            time.sleep(0.10)
        self._sync_progress(key, stage, progress_file)
        returncode = process.wait()
        log_handle.close()
        with self._lock:
            payload = self.query(*key) or {}
            cancelled = key in self._cancelled
            payload.update(
                {
                    "status": "cancelled" if cancelled else ("success" if returncode == 0 else "failed"),
                    "ended_at": _now_iso(),
                    "returncode": returncode,
                }
            )
            if not cancelled and returncode != 0:
                payload["error"] = f"command failed with return code {returncode}"
                payload["detail"] = _last_log_line(payload.get("log_file")) or payload["error"]
            status_store = JobStatusStore(self._run_dir(*key) / "job_status.json", run_id=key[1])
            if cancelled:
                status_store.update_stage(
                    self._status_stage(stage),
                    status="cancelled",
                    progress=0.0,
                    message="任务已取消",
                    log_file=payload.get("log_file"),
                    operation=stage,
                )
            elif returncode == 0:
                success_message = "路线拟合完成" if stage == "alignment" else "任务完成"
                if stage == "quality":
                    manifest_path = self._run_dir(*key) / "manifest.json"
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        manifest = {}
                    road_stage = next(
                        (
                            item
                            for item in manifest.get("stages", [])
                            if item.get("stage_name") == "road_surface"
                        ),
                        None,
                    )
                    if road_stage and road_stage.get("status") == "skipped":
                        success_message = "质量检测完成；当前 CAD 未检测到道路中心线，已跳过道路表面诊断"
                if stage == "sfm":
                    stats_path = self._run_dir(*key) / "02_sfm" / "sfm_stats.json"
                    try:
                        sfm_stats = json.loads(stats_path.read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        sfm_stats = {}
                    if sfm_stats.get("suitable_for_3d") is False:
                        success_message = _sfm_completion_message(sfm_stats)
                    elif sfm_stats.get("cpu_fallback"):
                        success_message = "SfM 完成，但 CUDA 未生效，已回退 CPU；请查看 SfM 报告"
                status_store.update_stage(
                    self._status_stage(stage),
                    status="success",
                    progress=1.0,
                    message=success_message,
                    log_file=payload.get("log_file"),
                    operation=stage,
                )
            else:
                status_store.update_stage(
                    self._status_stage(stage),
                    status="failed",
                    progress=0.0,
                    message="任务失败，请查看日志",
                    error=payload.get("detail", payload["error"]),
                    log_file=payload.get("log_file"),
                    operation=stage,
                )
            # `job_process.json` 是外部观察 terminal 状态的发布屏障；
            # 必须在对应 stage 已持久化后再切换，避免读到半发布结果。
            _atomic_json(self._process_path(*key), payload)
            self._processes.pop(key, None)
            self._cancelled.discard(key)

    def _sync_progress(
        self,
        key: tuple[str, str],
        stage: str,
        progress_file: str | Path | None,
    ) -> None:
        if progress_file is None:
            return
        try:
            progress = json.loads(Path(progress_file).read_text(encoding="utf-8-sig"))
            fraction = float(progress["fraction"])
            message = str(progress["message"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return
        if not 0.0 <= fraction < 1.0:
            return
        status_store = JobStatusStore(
            self._run_dir(*key) / "job_status.json", run_id=key[1]
        )
        try:
            current = status_store.load()
        except (OSError, json.JSONDecodeError):
            return
        stage_name = self._status_stage(stage)
        current_stage = current["stages"][stage_name]
        if current_stage.get("status") != "running":
            return
        if fraction <= float(current_stage.get("progress", 0.0)):
            return
        try:
            status_store.update_stage(
                stage_name,
                status="running",
                progress=min(fraction, 0.99),
                message=message,
                log_file=(self.query(*key) or {}).get("log_file"),
                operation=stage,
            )
        except (OSError, json.JSONDecodeError):
            return

    def cancel(self, dataset: str, run_id: str) -> dict[str, Any]:
        key = (_safe_name(dataset, "dataset"), _safe_name(run_id, "runId"))
        with self._lock:
            process = self._processes.get(key)
            payload = self.query(*key)
            if payload is None or payload.get("status") != "running":
                raise RuntimeError("没有正在运行的任务")
            self._cancelled.add(key)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            else:
                pid = int(payload.get("pid", 0))
                if pid > 0:
                    os.kill(pid, signal.SIGTERM)
            payload.update({"status": "cancelled", "ended_at": _now_iso(), "returncode": process.poll() if process else None})
            _atomic_json(self._process_path(*key), payload)
            JobStatusStore(self._run_dir(*key) / "job_status.json", run_id=key[1]).update_stage(
                self._status_stage(str(payload["stage"])),
                status="cancelled",
                progress=0.0,
                message="任务已取消",
                log_file=payload.get("log_file"),
                operation=str(payload["stage"]),
            )
            return payload

    def tail_log(self, dataset: str, run_id: str, stage: str, tail: int = 200) -> dict[str, Any]:
        if stage not in ALLOWED_STAGES:
            raise ValueError(f"unsupported workflow stage: {stage}")
        path = self._run_dir(dataset, run_id) / "logs" / "workflow" / f"{stage}.log"
        lines = read_workflow_log_text(path).splitlines() if path.exists() else []
        count = max(1, min(int(tail), 2000))
        return {"stage": stage, "log_file": str(path), "lines": lines[-count:]}

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def save_camera_track(root: str | Path, dataset: str, run_id: str, camera_track: Mapping[str, Any]) -> Path:
    base = Path(root).resolve()
    run_dir = base / "runs" / _safe_name(dataset, "dataset") / _safe_name(run_id, "runId")
    output = run_dir / "01_keyframes" / "camera_track_manual.json"
    unchanged = False
    if output.exists():
        try:
            unchanged = json.loads(output.read_text(encoding="utf-8-sig")) == dict(camera_track)
        except (OSError, json.JSONDecodeError):
            unchanged = False
    if not unchanged:
        _atomic_json(output, camera_track)
    plan_path = keyframe_plan_path(run_dir)
    if plan_path.exists():
        current_plan = load_keyframe_plan(plan_path)
        synced_plan = sync_keyframe_plan(current_plan, camera_track)
        if keyframe_plan_progress_changed(current_plan, synced_plan):
            write_keyframe_plan(plan_path, synced_plan)
    JobStatusStore(run_dir / "job_status.json", run_id=run_id).update_stage(
        "keyframes",
        status="success",
        progress=1.0,
        message="当前相机轨迹已保存",
    )
    return output
