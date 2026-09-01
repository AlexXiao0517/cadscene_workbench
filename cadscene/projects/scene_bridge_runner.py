from __future__ import annotations

import csv
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from cadscene.core.camera import CameraState
from cadscene.core.coordinates import python_state_to_web_camera
from .adapters import AdapterProgress, AdapterResult
from .scene_bridges import (
    FrameMap,
    SceneBridgeAnchor,
    SolveInterval,
    build_core_seed,
    remap_core_track_to_solve,
    select_overlap_anchors,
)


CommandRunner = Callable[[str, tuple[str, ...]], None]
BRIDGE_ALGORITHM_VERSION = "scene-overlap-precomputed-v2"


@dataclass(frozen=True)
class SceneBridgeInputs:
    identity: Mapping[str, object]
    application_root: Path
    attempt_directory: Path
    source_core_frame_map_path: Path
    source_manual_track_path: Path
    source_solve_video_path: Path
    source_solve_frame_map_path: Path
    source_solve_trajectory_path: Path
    source_sparse_ply_path: Path
    target_solve_video_path: Path
    target_solve_frame_map_path: Path
    target_solve_trajectory_path: Path
    target_solve_sparse_ply_path: Path
    target_core_video_path: Path
    target_core_frame_map_path: Path
    target_core_trajectory_path: Path
    target_core_sparse_ply_path: Path
    cad_dir: Path
    cad_scale: float
    origin_xy: tuple[float, float]
    overlap_seconds: Fraction = Fraction(4, 1)

    def __post_init__(self) -> None:
        required_identity = {
            "schema_version",
            "algorithm_version",
            "project_id",
            "source_clip_id",
            "target_clip_id",
            "direction",
            "operation_id",
        }
        if not required_identity.issubset(self.identity):
            raise ValueError("scene bridge identity is incomplete")
        if self.identity.get("algorithm_version") != BRIDGE_ALGORITHM_VERSION:
            raise ValueError("unsupported scene bridge algorithm version")
        if self.identity.get("direction") not in {"up", "down"}:
            raise ValueError("scene bridge direction must be up or down")
        if not math.isfinite(float(self.cad_scale)) or self.cad_scale <= 0:
            raise ValueError("scene bridge cad_scale must be positive")
        if len(self.origin_xy) != 2 or any(
            not math.isfinite(float(value)) for value in self.origin_xy
        ):
            raise ValueError("scene bridge origin_xy must contain two finite values")
        if self.overlap_seconds <= 0:
            raise ValueError("scene bridge overlap must be positive")

    def to_dict(self) -> dict[str, object]:
        path_fields = (
            "application_root",
            "attempt_directory",
            "source_core_frame_map_path",
            "source_manual_track_path",
            "source_solve_video_path",
            "source_solve_frame_map_path",
            "source_solve_trajectory_path",
            "source_sparse_ply_path",
            "target_solve_video_path",
            "target_solve_frame_map_path",
            "target_solve_trajectory_path",
            "target_solve_sparse_ply_path",
            "target_core_video_path",
            "target_core_frame_map_path",
            "target_core_trajectory_path",
            "target_core_sparse_ply_path",
            "cad_dir",
        )
        return {
            "identity": dict(self.identity),
            **{name: str(getattr(self, name)) for name in path_fields},
            "cad_scale": float(self.cad_scale),
            "origin_xy": [float(value) for value in self.origin_xy],
            "overlap_seconds": {
                "numerator": self.overlap_seconds.numerator,
                "denominator": self.overlap_seconds.denominator,
            },
        }

    def write_json(self, path: Path) -> None:
        _atomic_write_json(path, self.to_dict())

    @classmethod
    def from_json(cls, path: Path) -> SceneBridgeInputs:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise ValueError("scene bridge inputs must be a JSON object")
        identity = payload.get("identity")
        overlap = payload.get("overlap_seconds")
        origin = payload.get("origin_xy")
        if not isinstance(identity, Mapping):
            raise ValueError("scene bridge input identity is missing")
        if not isinstance(overlap, Mapping):
            raise ValueError("scene bridge overlap fraction is missing")
        if not isinstance(origin, list) or len(origin) != 2:
            raise ValueError("scene bridge origin_xy is missing")
        path_fields = (
            "application_root",
            "attempt_directory",
            "source_core_frame_map_path",
            "source_manual_track_path",
            "source_solve_video_path",
            "source_solve_frame_map_path",
            "source_solve_trajectory_path",
            "source_sparse_ply_path",
            "target_solve_video_path",
            "target_solve_frame_map_path",
            "target_solve_trajectory_path",
            "target_solve_sparse_ply_path",
            "target_core_video_path",
            "target_core_frame_map_path",
            "target_core_trajectory_path",
            "target_core_sparse_ply_path",
            "cad_dir",
        )
        paths: dict[str, Path] = {}
        for name in path_fields:
            value = payload.get(name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"scene bridge input path is missing: {name}")
            paths[name] = Path(value)
        return cls(
            identity=dict(identity),
            **paths,
            cad_scale=float(payload["cad_scale"]),
            origin_xy=(float(origin[0]), float(origin[1])),
            overlap_seconds=Fraction(
                _integer(overlap.get("numerator"), "overlap numerator"),
                _integer(overlap.get("denominator"), "overlap denominator"),
            ),
        )


def run_scene_bridge(
    inputs: SceneBridgeInputs,
    *,
    command_runner: CommandRunner | None = None,
) -> Path:
    _require_input_files(inputs)
    attempt = inputs.attempt_directory
    attempt.mkdir(parents=True, exist_ok=True)
    candidate = attempt / "candidate"
    if candidate.exists():
        raise FileExistsError(f"scene bridge candidate already exists: {candidate}")
    work = attempt / "scene_bridge_work"
    work.mkdir(parents=True, exist_ok=True)
    runner = command_runner or (
        lambda phase, command: _run_external_command(inputs, phase, command)
    )
    source_clip_id = str(inputs.identity["source_clip_id"])
    target_clip_id = str(inputs.identity["target_clip_id"])
    direction = str(inputs.identity["direction"])

    source_core_map = _load_frame_map(
        inputs.source_core_frame_map_path, source_clip_id
    )
    source_solve_map = _load_frame_map(
        inputs.source_solve_frame_map_path, source_clip_id
    )
    target_solve_map = _load_frame_map(
        inputs.target_solve_frame_map_path, target_clip_id
    )
    target_core_map = _load_frame_map(
        inputs.target_core_frame_map_path, target_clip_id
    )
    source_manual_track = json.loads(
        inputs.source_manual_track_path.read_text(encoding="utf-8-sig")
    )
    if not isinstance(source_manual_track, Mapping):
        raise ValueError("source manual camera track must be an object")
    source_solve_track = work / "source_solve_camera_track.json"
    _atomic_write_json(
        source_solve_track,
        remap_core_track_to_solve(
            source_manual_track, source_core_map, source_solve_map
        ),
    )

    source_alignment_root = work / "source_alignment"
    runner(
        "source_alignment",
        _alignment_command(
            inputs,
            dataset="source-bridge",
            run_id="source",
            output_root=source_alignment_root,
            trajectory=inputs.source_solve_trajectory_path,
            sparse_ply=inputs.source_sparse_ply_path,
            manual_track=source_solve_track,
            video=inputs.source_solve_video_path,
        ),
    )
    source_run = source_alignment_root / "source-bridge/source"
    source_path = source_run / "03_alignment/sfm_camera_path.csv"
    if not source_path.is_file():
        raise FileNotFoundError("source alignment camera path is missing")

    solve_registered, solve_fps = _trajectory_identity(
        inputs.target_solve_trajectory_path
    )
    source_cameras = _load_web_camera_path(
        source_path, cad_scale=inputs.cad_scale, origin_xy=inputs.origin_xy
    )
    anchors = select_overlap_anchors(
        direction=direction,
        source_core_map=source_solve_map,
        source_camera_path=source_cameras,
        target_solve_map=target_solve_map,
        target_registered_frames=solve_registered,
    )
    _write_progress(inputs, "overlap_anchors", "已选择两个共同 PTS 锚点", 0.72)
    solve_track = work / "target_solve_camera_track.json"
    _atomic_write_json(
        solve_track,
        _anchor_track(
            anchors,
            frame_map=target_solve_map,
            fps=solve_fps,
            direction=direction,
            source_clip_id=source_clip_id,
            target_clip_id=target_clip_id,
        ),
    )

    solve_alignment_root = work / "solve_alignment"
    runner(
        "solve_alignment",
        _alignment_command(
            inputs,
            dataset="target-bridge",
            run_id="solve",
            output_root=solve_alignment_root,
            trajectory=inputs.target_solve_trajectory_path,
            sparse_ply=inputs.target_solve_sparse_ply_path,
            manual_track=solve_track,
            video=inputs.target_solve_video_path,
        ),
    )
    solve_alignment_run = solve_alignment_root / "target-bridge/solve"
    solve_cameras = _load_web_camera_path(
        solve_alignment_run / "03_alignment/sfm_camera_path.csv",
        cad_scale=inputs.cad_scale,
        origin_xy=inputs.origin_xy,
    )
    core_registered, core_fps = _trajectory_identity(
        inputs.target_core_trajectory_path
    )
    core_seed = build_core_seed(
        core_map=target_core_map,
        core_registered_frames=core_registered,
        solve_map=target_solve_map,
        solve_camera_path=solve_cameras,
        fps=core_fps,
        direction=direction,
        source_clip_id=source_clip_id,
        target_clip_id=target_clip_id,
    )
    core_track = work / "target_core_camera_track.json"
    _atomic_write_json(core_track, core_seed)

    core_alignment_root = work / "core_alignment"
    runner(
        "core_alignment",
        _alignment_command(
            inputs,
            dataset="target-bridge",
            run_id="core",
            output_root=core_alignment_root,
            trajectory=inputs.target_core_trajectory_path,
            sparse_ply=inputs.target_core_sparse_ply_path,
            manual_track=core_track,
            video=inputs.target_core_video_path,
        ),
    )
    core_run = core_alignment_root / "target-bridge/core"
    solve_interval = _solve_interval_from_maps(target_solve_map, target_core_map)
    return _publish_candidate(
        inputs,
        candidate=candidate,
        core_track=core_track,
        core_run=core_run,
        solve_interval=solve_interval,
        anchors=anchors,
    )


def validate_scene_bridge_candidate(
    candidate_root: Path, expected_identity: Mapping[str, object]
) -> AdapterResult:
    root = Path(candidate_root)
    try:
        manifest_path = root / "scene_bridge_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if not isinstance(manifest, Mapping):
            raise ValueError("scene bridge manifest must be an object")
        if manifest.get("identity") != dict(expected_identity):
            raise ValueError("scene bridge candidate identity does not match")
        if manifest.get("status") != "awaiting_route_refinement":
            raise ValueError("scene bridge candidate status is invalid")
        anchor_pts = manifest.get("anchor_source_pts")
        if (
            not isinstance(anchor_pts, list)
            or len(anchor_pts) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in anchor_pts)
            or anchor_pts[0] == anchor_pts[1]
        ):
            raise ValueError("scene bridge anchors must contain two distinct integer PTS")
        if anchor_pts[0] > anchor_pts[1]:
            raise ValueError("scene bridge anchor PTS must be increasing")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise ValueError("scene bridge candidate artifacts are missing")
        paths: dict[str, Path] = {}
        artifact_identity: dict[str, dict[str, object]] = {}
        for name in (
            "camera_track",
            "alignment",
            "fitted_track",
            "camera_path",
            "viewer_scene",
        ):
            item = artifacts.get(name)
            if not isinstance(item, Mapping):
                raise ValueError(f"scene bridge artifact metadata is missing: {name}")
            relative = item.get("path")
            expected_hash = item.get("sha256")
            if not isinstance(relative, str) or Path(relative).is_absolute():
                raise ValueError(f"scene bridge artifact path is invalid: {name}")
            path = (root / relative).resolve(strict=False)
            path.relative_to(root.resolve(strict=True))
            if not path.is_file():
                raise ValueError(f"scene bridge artifact is missing: {name}")
            actual_hash = _file_sha256(path)
            if actual_hash != expected_hash:
                raise ValueError(f"scene bridge artifact hash mismatch: {name}")
            paths[name] = path
            artifact_identity[name] = {
                "path": relative,
                "sha256": actual_hash,
                "size_bytes": path.stat().st_size,
            }
            if item.get("size_bytes") != path.stat().st_size:
                raise ValueError(f"scene bridge artifact size mismatch: {name}")
        if (root / "core_alignment/04_quality").exists():
            raise ValueError("scene bridge candidate must stop before quality detection")
        seed = json.loads(paths["camera_track"].read_text(encoding="utf-8-sig"))
        keyframes = seed.get("keyframes") if isinstance(seed, Mapping) else None
        if not isinstance(keyframes, list) or len(keyframes) != 2:
            raise ValueError("scene bridge core seed must contain two anchors")
        frames = [item.get("frame") for item in keyframes if isinstance(item, Mapping)]
        if (
            len(frames) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in frames)
            or frames[0] >= frames[1]
            or any(item.get("source") != "scene_overlap_anchor" for item in keyframes)
        ):
            raise ValueError("scene bridge core seed anchors are invalid")
        proof_payload = {
            "identity": dict(expected_identity),
            "solve_interval": manifest.get("solve_interval"),
            "anchor_source_pts": anchor_pts,
            "artifacts": artifact_identity,
        }
        fingerprint = _json_sha256(proof_payload)
        revision = f"bridge-{fingerprint[:16]}"
        if (
            manifest.get("output_revision") != revision
            or manifest.get("output_fingerprint") != fingerprint
        ):
            raise ValueError("scene bridge output identity is invalid")
        return AdapterResult.success(
            output_revision=revision,
            output_fingerprint=fingerprint,
            outputs={
                "candidate_root": str(root),
                "manifest": str(manifest_path),
                **{name: str(path) for name, path in paths.items()},
            },
            progress=(
                AdapterProgress(
                    stage="validated",
                    message="scene bridge route fit validated",
                    fraction=1.0,
                ),
            ),
            validation_proof=proof_payload,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return AdapterResult.failed(str(exc))


def _require_input_files(inputs: SceneBridgeInputs) -> None:
    paths = (
        inputs.source_core_frame_map_path,
        inputs.source_manual_track_path,
        inputs.source_solve_video_path,
        inputs.source_solve_frame_map_path,
        inputs.source_solve_trajectory_path,
        inputs.source_sparse_ply_path,
        inputs.target_solve_video_path,
        inputs.target_solve_frame_map_path,
        inputs.target_solve_trajectory_path,
        inputs.target_solve_sparse_ply_path,
        inputs.target_core_video_path,
        inputs.target_core_frame_map_path,
        inputs.target_core_trajectory_path,
        inputs.target_core_sparse_ply_path,
        inputs.cad_dir / "design.json",
        inputs.application_root / "configs/pipelines/sfm_overlay_existing_sfm.yaml",
    )
    missing = next((path for path in paths if not path.is_file()), None)
    if missing is not None:
        raise FileNotFoundError(f"scene bridge input is missing: {missing}")


def _alignment_command(
    inputs: SceneBridgeInputs,
    *,
    dataset: str,
    run_id: str,
    output_root: Path,
    trajectory: Path,
    sparse_ply: Path,
    manual_track: Path,
    video: Path,
) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "cadscene.cli.run_pipeline",
        "--dataset",
        dataset,
        "--run-id",
        run_id,
        "--output-root",
        str(output_root),
        "--config",
        str(inputs.application_root / "configs/pipelines/sfm_overlay_existing_sfm.yaml"),
        "--stages",
        "alignment,viewer_scene",
        "--trajectory",
        str(trajectory),
        "--sparse-ply",
        str(sparse_ply),
        "--web-camera-track",
        str(manual_track),
        "--video",
        str(video),
        "--cad-dir",
        str(inputs.cad_dir),
        "--cad-scale",
        str(inputs.cad_scale),
        "--origin-xy",
        str(inputs.origin_xy[0]),
        str(inputs.origin_xy[1]),
    )


def _load_frame_map(path: Path, clip_id: str) -> FrameMap:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("frame map must be a JSON object")
    return FrameMap.from_dict(payload, clip_id=clip_id)


def _trajectory_identity(path: Path) -> tuple[set[int], float]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("trajectory must be a JSON object")
    poses = payload.get("poses")
    if not isinstance(poses, list):
        raise ValueError("trajectory contains no poses")
    registered: set[int] = set()
    for item in poses:
        if not isinstance(item, Mapping) or item.get("registered") is False:
            continue
        frame = item.get("frame_index")
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise ValueError("trajectory frame_index must be a non-negative integer")
        registered.add(frame)
    if len(registered) < 2:
        raise ValueError("trajectory contains fewer than two registered frames")
    fps = float(payload.get("fps", 0.0))
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("trajectory fps must be positive")
    return registered, fps


def _solve_interval_from_maps(
    solve_map: FrameMap, core_map: FrameMap
) -> SolveInterval:
    if solve_map.time_base != core_map.time_base:
        raise ValueError("target solve/core frame maps use different time bases")
    solve_index_by_pts = {
        entry.source_pts: entry.local_ordinal for entry in solve_map.frames
    }
    core_indices = [
        solve_index_by_pts.get(entry.source_pts) for entry in core_map.frames
    ]
    if any(index is None for index in core_indices):
        raise ValueError("target core PTS is missing from solve frame map")
    integer_indices = [int(index) for index in core_indices if index is not None]
    return SolveInterval(
        start_pts=solve_map.source_start_pts,
        end_pts_exclusive=solve_map.source_end_pts_exclusive,
        core_start_pts=core_map.source_start_pts,
        core_end_pts_exclusive=core_map.source_end_pts_exclusive,
        core_start_index=integer_indices[0],
        core_end_index_exclusive=integer_indices[-1] + 1,
        frame_pts=tuple(entry.source_pts for entry in solve_map.frames),
    )


def _load_web_camera_path(
    path: Path, *, cad_scale: float, origin_xy: Sequence[float]
) -> dict[int, dict[str, float]]:
    rows: dict[int, dict[str, float]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            if str(raw.get("status", "ok")).lower() != "ok":
                continue
            frame = int(float(raw["frame_index"]))
            state = CameraState.from_row(dict(raw), cad_scale=cad_scale)
            rows[frame] = python_state_to_web_camera(state, origin_xy)
    if len(rows) < 2:
        raise ValueError("aligned camera path contains fewer than two frames")
    return rows


def _anchor_track(
    anchors: Sequence[SceneBridgeAnchor],
    *,
    frame_map: FrameMap,
    fps: float,
    direction: str,
    source_clip_id: str,
    target_clip_id: str,
) -> dict[str, object]:
    return {
        "version": 1,
        "video": "",
        "fps": float(fps),
        "keyframes": [
            {
                "frame": item.target_frame,
                "time": float(
                    Fraction(item.source_pts - frame_map.source_start_pts)
                    * frame_map.time_base
                ),
                "source_pts": item.source_pts,
                "source": "scene_overlap_anchor",
                "camera": dict(item.camera),
            }
            for item in anchors
        ],
        "scene_bridge": {
            "direction": direction,
            "source_clip_id": source_clip_id,
            "target_clip_id": target_clip_id,
            "timestamp_authority": "source_decoded_frame_integer_pts",
        },
    }


def _publish_candidate(
    inputs: SceneBridgeInputs,
    *,
    candidate: Path,
    core_track: Path,
    core_run: Path,
    solve_interval,
    anchors: Sequence[SceneBridgeAnchor],
) -> Path:
    alignment_source = core_run / "03_alignment"
    viewer_source = core_run / "05_viewer_scene"
    required = (
        alignment_source / "alignment.json",
        alignment_source / "camera_track_pred.json",
        alignment_source / "sfm_camera_path.csv",
        viewer_source / "sfm_viewer_scene.json",
    )
    missing = next((path for path in required if not path.is_file()), None)
    if missing is not None:
        raise FileNotFoundError(f"core route fit output is missing: {missing}")
    target_core_map = _load_frame_map(
        inputs.target_core_frame_map_path,
        str(inputs.identity["target_clip_id"]),
    )
    temporary = Path(
        tempfile.mkdtemp(prefix=".candidate-", dir=inputs.attempt_directory)
    )
    try:
        shutil.copy2(core_track, temporary / "camera_track_seed.json")
        target_core = temporary / "core_alignment"
        shutil.copytree(alignment_source, target_core / "03_alignment")
        shutil.copytree(viewer_source, target_core / "05_viewer_scene")
        artifact_paths = {
            "camera_track": temporary / "camera_track_seed.json",
            "alignment": target_core / "03_alignment/alignment.json",
            "fitted_track": target_core
            / "03_alignment/camera_track_pred.json",
            "camera_path": target_core / "03_alignment/sfm_camera_path.csv",
            "viewer_scene": target_core / "05_viewer_scene/sfm_viewer_scene.json",
        }
        artifacts = {
            name: {
                "path": path.relative_to(temporary).as_posix(),
                "sha256": _file_sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for name, path in artifact_paths.items()
        }
        proof = {
            "identity": dict(inputs.identity),
            "solve_interval": {
                "source_start_pts": solve_interval.start_pts,
                "source_end_pts_exclusive": solve_interval.end_pts_exclusive,
                "core_start_pts": solve_interval.core_start_pts,
                "core_end_pts_exclusive": solve_interval.core_end_pts_exclusive,
                "time_base": {
                    "numerator": target_core_map.time_base.numerator,
                    "denominator": target_core_map.time_base.denominator,
                },
            },
            "anchor_source_pts": [item.source_pts for item in anchors],
            "artifacts": artifacts,
        }
        fingerprint = _json_sha256(proof)
        revision = f"bridge-{fingerprint[:16]}"
        _atomic_write_json(
            temporary / "scene_bridge_manifest.json",
            {
                "schema_version": 1,
                "status": "awaiting_route_refinement",
                **proof,
                "output_revision": revision,
                "output_fingerprint": fingerprint,
            },
        )
        os.replace(temporary, candidate)
        temporary = None
        _write_progress(inputs, "validated", "路线拟合已验证，等待人工微调", 0.99)
        return candidate
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)


def _run_external_command(
    inputs: SceneBridgeInputs, phase: str, command: tuple[str, ...]
) -> None:
    spans = {
        "source_alignment": (0.02, 0.32, "正在验证源片段路线"),
        "solve_alignment": (0.38, 0.70, "正在用共同 PTS 拟合目标路线"),
        "core_alignment": (0.74, 0.97, "正在生成核心片段微调路线"),
    }
    start, end, message = spans[phase]
    _write_progress(inputs, phase, message, start)
    nested_progress = None
    if "--progress-file" in command:
        nested_progress = Path(command[command.index("--progress-file") + 1])
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    process = subprocess.Popen(command, **options)
    last_serialized: str | None = None
    while process.poll() is None:
        if nested_progress is not None and nested_progress.is_file():
            try:
                serialized = nested_progress.read_text(encoding="utf-8")
                if serialized != last_serialized:
                    payload = json.loads(serialized)
                    fraction = float(payload.get("fraction", 0.0))
                    scaled = start + max(0.0, min(1.0, fraction)) * (end - start)
                    _write_progress(
                        inputs,
                        phase,
                        str(payload.get("message") or message),
                        min(end, scaled),
                    )
                    last_serialized = serialized
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        time.sleep(0.25)
    if process.returncode != 0:
        raise RuntimeError(f"scene bridge {phase} failed with return code {process.returncode}")
    _write_progress(inputs, phase, message, end)


def _write_progress(
    inputs: SceneBridgeInputs, stage: str, message: str, fraction: float
) -> None:
    _atomic_write_json(
        inputs.attempt_directory / "adapter_progress.json",
        {
            "schema_version": "1.0",
            "stage": stage,
            "message": message,
            "fraction": max(0.0, min(0.99, float(fraction))),
        },
    )


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(payload: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value
