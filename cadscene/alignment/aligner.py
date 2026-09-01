from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from cadscene.alignment.keyframes import confirmed_keyframes, load_web_camera_track
from cadscene.core.camera import (
    CameraState,
    camera_to_world_rotation,
    decompose_world_from_camera_rotation,
)
from cadscene.core.coordinates import python_state_to_web_camera, web_camera_to_python_state
from cadscene.core.io import read_json
from cadscene.core.sim3 import Sim3
from cadscene.sfm.trajectory import SfmTrajectory, load_sfm_trajectory


MAX_GLOBAL_ANCHOR_RESIDUAL_M = 10.0
MAX_BASELINE_DIRECTION_ERROR_DEG = 0.1
MAX_METRIC_CORRECTION_ANGLE_RESIDUAL_DEG = 15.0
MAX_FOCAL_ASPECT_RATIO = 2.0
UPSTREAM_SFM_MANUAL_FOV_WARNING = (
    "Upstream SfM intrinsics/geometry are unreliable; manual FOV is being used."
)
INDEPENDENT_FY_CAMERA_MODELS = frozenset(
    {
        "PINHOLE",
        "OPENCV",
        "OPENCV_FISHEYE",
        "FULL_OPENCV",
        "FOV",
        "THIN_PRISM_FISHEYE",
        "RAD_TAN_THIN_PRISM_FISHEYE",
        "DIVISION",
        "FISHEYE",
        "EUCM",
    }
)


@dataclass(frozen=True)
class AlignmentConfig:
    cad_scale: float = 1.0
    origin_xy: tuple[float, float] = (0.0, 0.0)
    fov: float = 70.0
    fov_from: str = "trajectory"
    frontend_track_step: int = 10
    frame_step: int = 1
    start_frame: int | None = None
    end_frame: int | None = None


@dataclass(frozen=True)
class KeyframeCorrespondence:
    frame_index: int
    state: CameraState
    center_cad: np.ndarray
    center_sfm: np.ndarray
    r_camfromworld_sfm: np.ndarray
    source: str

    def to_row(self) -> dict:
        return {
            "frame_index": int(self.frame_index),
            "source": self.source,
            "camera_x": float(self.state.camera_x),
            "camera_y": float(self.state.camera_y),
            "camera_z": float(self.state.camera_z),
            "yaw": float(self.state.yaw_deg),
            "pitch": float(self.state.pitch_deg),
            "roll": float(self.state.roll_deg),
            "sfm_x": float(self.center_sfm[0]),
            "sfm_y": float(self.center_sfm[1]),
            "sfm_z": float(self.center_sfm[2]),
        }


@dataclass(frozen=True)
class AnchoredAlignment:
    sim3: Sim3
    frames: np.ndarray
    residual_positions: np.ndarray
    residual_angles_deg: np.ndarray
    position_mode: str = "sfm_residual"
    anchor_positions: np.ndarray | None = None

    def residual_at(self, frame_index: float) -> tuple[np.ndarray, np.ndarray]:
        f = float(frame_index)
        if len(self.frames) == 0:
            return np.zeros(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
        if f <= self.frames[0]:
            return self.residual_positions[0].copy(), self.residual_angles_deg[0].copy()
        if f >= self.frames[-1]:
            return self.residual_positions[-1].copy(), self.residual_angles_deg[-1].copy()
        j = int(np.searchsorted(self.frames, f, side="right"))
        i = j - 1
        fa, fb = float(self.frames[i]), float(self.frames[j])
        alpha = (f - fa) / max(fb - fa, 1e-9)
        pos = self.residual_positions[i] * (1.0 - alpha) + self.residual_positions[j] * alpha
        ang = self.residual_angles_deg[i] * (1.0 - alpha) + self.residual_angles_deg[j] * alpha
        return pos, ang

    def residual_summary(self) -> dict:
        mag = np.linalg.norm(self.residual_positions, axis=1) if len(self.residual_positions) else np.asarray([])
        return {
            "num_anchors": int(len(self.frames)),
            "residual_pos_m_max": float(mag.max()) if len(mag) else 0.0,
            "residual_pos_m_mean": float(mag.mean()) if len(mag) else 0.0,
            "alignment_mode": self.position_mode,
            "scale_observable": self.position_mode not in {
                "rotation_only",
                "metric_direct",
            },
        }

    def anchor_position_at(self, frame_index: float) -> np.ndarray | None:
        if self.anchor_positions is None or len(self.anchor_positions) == 0:
            return None
        f = float(frame_index)
        if f <= self.frames[0]:
            return self.anchor_positions[0].copy()
        if f >= self.frames[-1]:
            return self.anchor_positions[-1].copy()
        j = int(np.searchsorted(self.frames, f, side="right"))
        i = j - 1
        fa, fb = float(self.frames[i]), float(self.frames[j])
        alpha = (f - fa) / max(fb - fa, 1e-9)
        return self.anchor_positions[i] * (1.0 - alpha) + self.anchor_positions[j] * alpha


@dataclass(frozen=True)
class AlignmentResult:
    alignment_json: dict
    sfm_camera_path_rows: list[dict]
    camera_track_pred: dict
    keyframe_correspondences: list[dict]
    metrics: dict


def _normalize_angle(degrees: float) -> float:
    return (float(degrees) + 180.0) % 360.0 - 180.0


def _camera_to_world_rotation(state: CameraState) -> np.ndarray:
    """相机坐标系 x=右、y=下、z=前；pitch 为 Python 后端向下角。"""

    return camera_to_world_rotation(state)


def _decompose_world_from_cam(rotation: np.ndarray) -> tuple[float, float, float]:
    return decompose_world_from_camera_rotation(rotation)


def _fov_for_path(traj: SfmTrajectory, config: AlignmentConfig) -> float:
    if config.fov_from == "trajectory":
        return float(traj.horizontal_fov_deg() or config.fov)
    return _validated_configured_fov(config.fov)


def _validated_configured_fov(value: object) -> float:
    try:
        fov = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"configured FOV must be finite and inside (1, 179): {value!r}") from exc
    if not math.isfinite(fov) or not 1.0 < fov < 179.0:
        raise RuntimeError(f"configured FOV must be finite and inside (1, 179): {value!r}")
    return fov


def _manual_fov_from_track(track: Mapping[str, object]) -> float | None:
    values: list[float] = []
    for keyframe in confirmed_keyframes(dict(track)):
        camera = keyframe.get("camera") or {}
        try:
            fov = float(camera["fov"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(fov) or not 1.0 < fov < 179.0:
            return None
        values.append(fov)
    if not values or max(values) - min(values) > 0.1:
        return None
    return float(sum(values) / len(values))


def _trajectory_intrinsics_warning(traj: SfmTrajectory) -> str | None:
    params = traj.intrinsics.get("params", [])
    try:
        fx = float(params[0])
    except (IndexError, TypeError, ValueError):
        return "pathological intrinsics: focal ratio unavailable because fx is missing"
    if not math.isfinite(fx) or fx <= 0.0:
        return "pathological intrinsics: focal ratio unavailable because fx is not finite and positive"

    model = str(traj.intrinsics.get("model", "")).upper()
    if model not in INDEPENDENT_FY_CAMERA_MODELS:
        return None
    try:
        fy = float(params[1])
    except (IndexError, TypeError, ValueError):
        return "pathological intrinsics: focal ratio unavailable because fy is missing"
    if not math.isfinite(fy) or fy <= 0.0:
        return "pathological intrinsics: focal ratio unavailable because fy is not finite and positive"
    ratio = max(fx, fy) / min(fx, fy)
    if ratio > MAX_FOCAL_ASPECT_RATIO:
        return (
            "pathological intrinsics: focal ratio "
            f"{ratio:.12g} exceeds {MAX_FOCAL_ASPECT_RATIO:.12g}"
        )
    return None


def _alignment_validation_warning(
    *,
    fov_source: str,
    intrinsics_warning: str | None,
) -> str | None:
    if intrinsics_warning is None:
        return None
    if fov_source == "manual":
        return UPSTREAM_SFM_MANUAL_FOV_WARNING
    return intrinsics_warning


def _validate_alignment_result(
    metrics: Mapping[str, object],
    *,
    intrinsics_warning: str | None,
    trusted_fov: bool,
) -> None:
    global_residual = float(metrics.get("global_residual_m_max", 0.0))
    scale_observable = bool(metrics.get("scale_observable", True))
    if scale_observable and (
        not math.isfinite(global_residual)
        or global_residual > MAX_GLOBAL_ANCHOR_RESIDUAL_M
    ):
        raise RuntimeError(
            "global anchor residual exceeds "
            f"{MAX_GLOBAL_ANCHOR_RESIDUAL_M:.12g} m: {global_residual:.12g} m"
        )
    if scale_observable and "baseline_direction_error_deg" in metrics:
        baseline_direction_error = float(metrics["baseline_direction_error_deg"])
        if (
            not math.isfinite(baseline_direction_error)
            or baseline_direction_error > MAX_BASELINE_DIRECTION_ERROR_DEG
        ):
            raise RuntimeError(
                "baseline direction error exceeds "
                f"{MAX_BASELINE_DIRECTION_ERROR_DEG:.12g} deg: "
                f"{baseline_direction_error:.12g} deg"
            )
    if intrinsics_warning is not None and not trusted_fov:
        raise RuntimeError(
            f"{intrinsics_warning}; provide a confirmed manual or configured FOV"
        )


def build_correspondences(
    track: Mapping[str, object],
    traj: SfmTrajectory,
    config: AlignmentConfig,
    *,
    allow_empty: bool = False,
) -> list[KeyframeCorrespondence]:
    out: list[KeyframeCorrespondence] = []
    for keyframe in confirmed_keyframes(dict(track)):
        camera = keyframe.get("camera") or {}
        if not all(name in camera for name in ("x", "y", "z")):
            continue
        frame_index = int(keyframe.get("frame", 0))
        state = web_camera_to_python_state(camera, config.origin_xy, config.cad_scale)
        center_sfm, r_camfromworld_sfm = traj.query(frame_index)
        out.append(
            KeyframeCorrespondence(
                frame_index=frame_index,
                state=state,
                center_cad=np.asarray([state.camera_x, state.camera_y, state.camera_z], dtype=np.float64),
                center_sfm=np.asarray(center_sfm, dtype=np.float64),
                r_camfromworld_sfm=np.asarray(r_camfromworld_sfm, dtype=np.float64),
                source=str(keyframe.get("source") or "manual_keyframe"),
            )
        )
    out.sort(key=lambda row: row.frame_index)
    if not out and not allow_empty:
        raise RuntimeError("camera_track.json 中没有可用于对齐的人工或已确认关键帧。")
    return out


def _is_metric_direct_trajectory(traj: SfmTrajectory) -> bool:
    return bool(
        traj.meta.get("coordinate_system") == "cad_local_m"
        and traj.meta.get("metric_scale_locked") is True
    )


def _wrapped_mean_degrees(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    radians = np.radians(np.asarray(values, dtype=np.float64))
    return _normalize_angle(
        math.degrees(
            math.atan2(float(np.mean(np.sin(radians))), float(np.mean(np.cos(radians))))
        )
    )


def _metric_direct_alignment(
    correspondences: Sequence[KeyframeCorrespondence],
    traj: SfmTrajectory,
    config: AlignmentConfig,
) -> tuple[Sim3, AnchoredAlignment]:
    if not correspondences:
        return Sim3.identity(), AnchoredAlignment(
            sim3=Sim3.identity(),
            frames=np.asarray([], dtype=np.int64),
            residual_positions=np.empty((0, 3), dtype=np.float64),
            residual_angles_deg=np.empty((0, 3), dtype=np.float64),
            position_mode="metric_direct",
            anchor_positions=None,
        )
    translations = np.asarray(
        [row.center_cad - row.center_sfm for row in correspondences],
        dtype=np.float64,
    )
    translation = np.median(translations, axis=0)
    position_residuals = np.linalg.norm(translations - translation, axis=1)
    if float(position_residuals.max()) > MAX_GLOBAL_ANCHOR_RESIDUAL_M:
        raise RuntimeError(
            "metric-direct translation corrections disagree by more than "
            f"{MAX_GLOBAL_ANCHOR_RESIDUAL_M:g} m"
        )
    identity = Sim3(
        scale=1.0,
        rotation=np.eye(3, dtype=np.float64),
        translation=translation,
    )
    angle_deltas: list[list[float]] = []
    for row in correspondences:
        source = _global_state_at_frame(row.frame_index, traj, identity, config)
        angle_deltas.append(
            [
                _normalize_angle(row.state.yaw_deg - source.yaw_deg),
                _normalize_angle(row.state.pitch_deg - source.pitch_deg),
                _normalize_angle(row.state.roll_deg - source.roll_deg),
            ]
        )
    angle_offset = np.asarray(
        [
            _wrapped_mean_degrees([row[index] for row in angle_deltas])
            for index in range(3)
        ],
        dtype=np.float64,
    )
    angular_residuals = np.asarray(
        [
            [_normalize_angle(row[index] - angle_offset[index]) for index in range(3)]
            for row in angle_deltas
        ],
        dtype=np.float64,
    )
    if angular_residuals.size and float(np.max(np.abs(angular_residuals))) > MAX_METRIC_CORRECTION_ANGLE_RESIDUAL_DEG:
        raise RuntimeError(
            "metric-direct attitude corrections are inconsistent across anchors"
        )
    anchored = AnchoredAlignment(
        sim3=identity,
        frames=np.asarray(
            [row.frame_index for row in correspondences], dtype=np.int64
        ),
        residual_positions=np.zeros((len(correspondences), 3), dtype=np.float64),
        residual_angles_deg=np.tile(angle_offset, (len(correspondences), 1)),
        position_mode="metric_direct",
        anchor_positions=None,
    )
    return identity, anchored


def estimate_global_sim3(correspondences: Sequence[KeyframeCorrespondence]) -> Sim3:
    if len(correspondences) == 2:
        dst = np.asarray([row.center_cad for row in correspondences], dtype=np.float64)
        if _position_spread(dst) > 1e-6:
            return _estimate_two_anchor_sim3(correspondences)
    return _estimate_global_sim3_from_oriented_keyframes(correspondences)


def _position_spread(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    return max(
        float(np.linalg.norm(points[i] - points[j]))
        for i in range(len(points))
        for j in range(i + 1, len(points))
    )


def _estimate_two_anchor_sim3(correspondences: Sequence[KeyframeCorrespondence]) -> Sim3:
    if len(correspondences) != 2:
        raise RuntimeError("two-anchor sim3 requires exactly two keyframe correspondences")

    src_first, src_second = (
        np.asarray(row.center_sfm, dtype=np.float64)
        for row in correspondences
    )
    dst_first, dst_second = (
        np.asarray(row.center_cad, dtype=np.float64)
        for row in correspondences
    )
    src_baseline = src_second - src_first
    dst_baseline = dst_second - dst_first
    src_length = float(np.linalg.norm(src_baseline))
    dst_length = float(np.linalg.norm(dst_baseline))
    if src_length <= 1e-9 or dst_length <= 1e-9:
        raise RuntimeError("keyframe centers are degenerate; cannot estimate two-anchor sim3")

    src_direction = src_baseline / src_length
    dst_direction = dst_baseline / dst_length
    cosine = float(np.clip(np.dot(src_direction, dst_direction), -1.0, 1.0))
    cross = np.cross(src_direction, dst_direction)
    cross_norm = float(np.linalg.norm(cross))
    machine_epsilon = np.finfo(np.float64).eps
    shortest_rotation: np.ndarray | None = None
    # Subtracting translated float64 endpoints can amplify exact-antiparallel
    # roundoff above a few eps; 32 eps is the narrow boundary covered by that case.
    if cosine < 0.0 and cross_norm <= 32.0 * machine_epsilon:
        basis = np.zeros(3, dtype=np.float64)
        basis[int(np.argmin(np.abs(src_direction)))] = 1.0
        axis = np.cross(src_direction, basis)
        axis /= np.linalg.norm(axis)
        quaternion = np.concatenate(([0.0], axis))
    elif cosine < 0.0:
        # A threshold between the frame and quaternion constructions creates a
        # numerical cliff near antiparallel baselines. The frame map is stable
        # throughout this hemisphere and still maps the measured directions
        # exactly, so it preserves rather than discards their true deviation.
        basis = np.zeros(3, dtype=np.float64)
        basis[
            int(
                np.argmin(
                    np.maximum(np.abs(src_direction), np.abs(dst_direction))
                )
            )
        ] = 1.0
        src_perpendicular = basis - src_direction * float(
            np.dot(src_direction, basis)
        )
        src_perpendicular /= np.linalg.norm(src_perpendicular)
        dst_perpendicular = basis - dst_direction * float(
            np.dot(dst_direction, basis)
        )
        dst_perpendicular /= np.linalg.norm(dst_perpendicular)
        src_frame = np.column_stack(
            [
                src_direction,
                src_perpendicular,
                np.cross(src_direction, src_perpendicular),
            ]
        )
        dst_frame = np.column_stack(
            [
                dst_direction,
                dst_perpendicular,
                np.cross(dst_direction, dst_perpendicular),
            ]
        )
        shortest_rotation = dst_frame @ src_frame.T
        quaternion = None
    elif cross_norm <= 4.0 * machine_epsilon:
        quaternion = np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float64)
    else:
        axis = cross / cross_norm
        half_angle = 0.5 * math.atan2(cross_norm, cosine)
        quaternion = np.concatenate(
            ([math.cos(half_angle)], math.sin(half_angle) * axis)
        )
        quaternion /= np.linalg.norm(quaternion)
    if shortest_rotation is None:
        w, x, y, z = quaternion
        shortest_rotation = np.asarray(
            [
                [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
                [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
                [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
            ]
        )

    target_sum = np.sum(
        np.asarray(
            [
                _camera_to_world_rotation(row.state)
                @ row.r_camfromworld_sfm
                @ shortest_rotation.T
                for row in correspondences
            ],
            dtype=np.float64,
        ),
        axis=0,
    )
    twist_generator = np.asarray(
        [
            [0.0, -dst_direction[2], dst_direction[1]],
            [dst_direction[2], 0.0, -dst_direction[0]],
            [-dst_direction[1], dst_direction[0], 0.0],
        ],
        dtype=np.float64,
    )
    cosine_weight = float(
        np.trace(target_sum)
        - dst_direction @ target_sum @ dst_direction
    )
    sine_weight = float(-np.trace(twist_generator @ target_sum))
    twist = math.atan2(sine_weight, cosine_weight)
    twist_rotation = (
        np.eye(3, dtype=np.float64)
        + math.sin(twist) * twist_generator
        + (1.0 - math.cos(twist)) * (twist_generator @ twist_generator)
    )
    rotation = twist_rotation @ shortest_rotation
    scale = dst_length / src_length
    translation = dst_first - scale * (rotation @ src_first)
    return Sim3(scale=scale, rotation=rotation, translation=translation)


def _estimate_global_sim3_from_oriented_keyframes(correspondences: Sequence[KeyframeCorrespondence]) -> Sim3:
    """按旧版的关键帧姿态约束估计全局 Sim3。"""
    if len(correspondences) < 2:
        raise RuntimeError("global sim3 requires at least two keyframe correspondences")

    # 旧版先平均每个关键帧推导出的世界旋转，再以中心距离中位数确定尺度。
    # 两个位置点本身不能约束绕连线方向的转角，不能用位置 SVD 替代这里的姿态约束。
    align_matrices = [
        _camera_to_world_rotation(row.state) @ row.r_camfromworld_sfm
        for row in correspondences
    ]
    accumulator = np.sum(np.asarray(align_matrices, dtype=np.float64), axis=0)
    u, _singular_values, vt = np.linalg.svd(accumulator)
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1.0
        rotation = u @ vt

    src = np.asarray([row.center_sfm for row in correspondences], dtype=np.float64)
    dst = np.asarray([row.center_cad for row in correspondences], dtype=np.float64)
    distance_ratios: list[float] = []
    cad_position_spread = _position_spread(dst)
    for i in range(len(correspondences)):
        for j in range(i + 1, len(correspondences)):
            sfm_distance = float(np.linalg.norm(src[i] - src[j]))
            cad_distance = float(np.linalg.norm(dst[i] - dst[j]))
            # 重合的人工位置不提供尺度约束，不能把对应比值 0 纳入中位数。
            if sfm_distance > 1e-9 and cad_distance > 1e-6:
                distance_ratios.append(cad_distance / sfm_distance)
    if cad_position_spread <= 1e-6:
        # 纯旋转视频没有平移基线，尺度不可观测；1.0 仅用于保持 Sim3 输出协议。
        scale = 1.0
    elif not distance_ratios:
        raise RuntimeError("SfM keyframe centers are degenerate; cannot estimate sim3")
    else:
        scale = float(np.median(distance_ratios))
    translation = np.mean(dst - scale * (src @ rotation.T), axis=0)
    return Sim3(scale=scale, rotation=rotation, translation=translation)


def _legacy_position_only_sim3(correspondences: Sequence[KeyframeCorrespondence]) -> Sim3:
    if len(correspondences) < 2:
        raise RuntimeError("global sim3 至少需要 2 个关键帧对应。")
    src = np.asarray([row.center_sfm for row in correspondences], dtype=np.float64)
    dst = np.asarray([row.center_cad for row in correspondences], dtype=np.float64)
    src_mean = src.mean(axis=0)
    dst_mean = dst.mean(axis=0)
    src_centered = src - src_mean
    dst_centered = dst - dst_mean
    covariance = (dst_centered.T @ src_centered) / len(src)
    u, singular_values, vt = np.linalg.svd(covariance)
    correction = np.eye(3, dtype=np.float64)
    if np.linalg.det(u @ vt) < 0:
        correction[-1, -1] = -1.0
    rotation = u @ correction @ vt
    variance = float(np.mean(np.sum(src_centered * src_centered, axis=1)))
    if variance <= 1e-12:
        raise RuntimeError("SfM 关键帧中心退化，无法估计 sim3。")
    scale = float(np.sum(singular_values * np.diag(correction)) / variance)
    translation = dst_mean - scale * (rotation @ src_mean)
    return Sim3(scale=scale, rotation=rotation, translation=translation)


def _global_state_at_frame(frame_index: float, traj: SfmTrajectory, sim3: Sim3, config: AlignmentConfig) -> CameraState:
    center_sfm, r_camfromworld_sfm = traj.query(frame_index)
    center_cad = sim3.apply_point(center_sfm)
    world_from_cam_cad = sim3.rotate_world_from_cam(r_camfromworld_sfm)
    yaw, pitch, roll = _decompose_world_from_cam(world_from_cam_cad)
    return CameraState(
        camera_x=float(center_cad[0]),
        camera_y=float(center_cad[1]),
        camera_z=float(center_cad[2]),
        yaw_deg=float(yaw),
        pitch_deg=float(pitch),
        roll_deg=float(roll),
        fov_deg=_fov_for_path(traj, config),
        cad_scale=float(config.cad_scale),
    )


def apply_segment_anchoring(
    sim3: Sim3,
    correspondences: Sequence[KeyframeCorrespondence],
    traj: SfmTrajectory,
    config: AlignmentConfig,
) -> AnchoredAlignment:
    frames: list[int] = []
    res_pos: list[np.ndarray] = []
    res_ang: list[np.ndarray] = []
    anchor_positions: list[np.ndarray] = []
    for corr in sorted(correspondences, key=lambda row: row.frame_index):
        global_state = _global_state_at_frame(corr.frame_index, traj, sim3, config)
        frames.append(int(corr.frame_index))
        anchor_positions.append(corr.center_cad.copy())
        res_pos.append(
            np.asarray(
                [
                    corr.state.camera_x - global_state.camera_x,
                    corr.state.camera_y - global_state.camera_y,
                    corr.state.camera_z - global_state.camera_z,
                ],
                dtype=np.float64,
            )
        )
        res_ang.append(
            np.asarray(
                [
                    _normalize_angle(corr.state.yaw_deg - global_state.yaw_deg),
                    _normalize_angle(corr.state.pitch_deg - global_state.pitch_deg),
                    _normalize_angle(corr.state.roll_deg - global_state.roll_deg),
                ],
                dtype=np.float64,
            )
        )
    anchor_position_array = np.asarray(anchor_positions, dtype=np.float64)
    position_mode = "rotation_only" if _position_spread(anchor_position_array) <= 1e-6 else "sfm_residual"
    return AnchoredAlignment(
        sim3=sim3,
        frames=np.asarray(frames, dtype=np.int64),
        residual_positions=np.asarray(res_pos, dtype=np.float64),
        residual_angles_deg=np.asarray(res_ang, dtype=np.float64),
        position_mode=position_mode,
        anchor_positions=anchor_position_array,
    )


def _apply_residual(global_state: CameraState, residual_pos: np.ndarray, residual_ang: np.ndarray) -> CameraState:
    return CameraState(
        camera_x=float(global_state.camera_x + residual_pos[0]),
        camera_y=float(global_state.camera_y + residual_pos[1]),
        camera_z=float(global_state.camera_z + residual_pos[2]),
        yaw_deg=float(global_state.yaw_deg + residual_ang[0]),
        pitch_deg=float(global_state.pitch_deg + residual_ang[1]),
        roll_deg=float(global_state.roll_deg + residual_ang[2]),
        fov_deg=float(global_state.fov_deg),
        cad_scale=float(global_state.cad_scale),
    )


def aligned_state_at_frame(
    frame_index: float,
    traj: SfmTrajectory,
    sim3: Sim3,
    anchored: AnchoredAlignment | None,
    config: AlignmentConfig,
) -> CameraState:
    global_state = _global_state_at_frame(frame_index, traj, sim3, config)
    if anchored is None:
        return global_state
    residual_pos, residual_ang = anchored.residual_at(frame_index)
    state = _apply_residual(global_state, residual_pos, residual_ang)
    if anchored.position_mode != "rotation_only":
        return state
    anchored_position = anchored.anchor_position_at(frame_index)
    if anchored_position is None:
        return state
    return CameraState(
        camera_x=float(anchored_position[0]),
        camera_y=float(anchored_position[1]),
        camera_z=float(anchored_position[2]),
        yaw_deg=state.yaw_deg,
        pitch_deg=state.pitch_deg,
        roll_deg=state.roll_deg,
        fov_deg=state.fov_deg,
        cad_scale=state.cad_scale,
    )


def generate_aligned_camera_path(
    traj: SfmTrajectory,
    sim3: Sim3,
    anchored: AnchoredAlignment | None,
    config: AlignmentConfig,
) -> list[dict]:
    start = int(config.start_frame if config.start_frame is not None else traj.frame_min)
    end = int(config.end_frame if config.end_frame is not None else traj.frame_max)
    step = max(1, int(config.frame_step))
    rows: list[dict] = []
    for frame in range(start, end + 1, step):
        state = aligned_state_at_frame(frame, traj, sim3, anchored, config)
        row = state.to_row(frame_index=frame, status="ok")
        row["path_source"] = (
            "metric_direct"
            if anchored is not None and anchored.position_mode == "metric_direct"
            else "rotation_only_anchor"
            if anchored is not None and anchored.position_mode == "rotation_only"
            else "segment_anchor" if anchored is not None else "global_sim3"
        )
        rows.append(row)
    if rows and rows[-1]["frame_index"] != end:
        state = aligned_state_at_frame(end, traj, sim3, anchored, config)
        row = state.to_row(frame_index=end, status="ok")
        row["path_source"] = (
            "metric_direct"
            if anchored is not None and anchored.position_mode == "metric_direct"
            else "rotation_only_anchor"
            if anchored is not None and anchored.position_mode == "rotation_only"
            else "segment_anchor" if anchored is not None else "global_sim3"
        )
        rows.append(row)
    return rows


def generate_camera_track_pred(
    track: Mapping[str, object],
    path_rows: Sequence[Mapping[str, object]],
    config: AlignmentConfig,
    *,
    fps: float | None = None,
) -> dict:
    kept = []
    manual_frames: set[int] = set()
    for keyframe in confirmed_keyframes(dict(track)):
        item = dict(keyframe)
        frame = int(item.get("frame", 0))
        manual_frames.add(frame)
        kept.append(item)
    step = max(1, int(config.frontend_track_step))
    for row in path_rows:
        frame = int(row["frame_index"])
        if frame in manual_frames or frame % step != 0:
            continue
        state = CameraState.from_row(dict(row), cad_scale=config.cad_scale)
        kept.append(
            {
                "frame": frame,
                "source": "algorithm_prediction",
                "camera": python_state_to_web_camera(state, config.origin_xy),
            }
        )
    kept.sort(key=lambda item: (int(item.get("frame", 0)), str(item.get("source", ""))))
    out = dict(track)
    out["keyframes"] = kept
    effective_fps = float(fps if fps is not None else out.get("fps") or 0.0)
    if math.isfinite(effective_fps) and effective_fps > 0.0:
        out["fps"] = effective_fps
        for keyframe in kept:
            keyframe["time"] = int(keyframe.get("frame", 0)) / effective_fps
    out["schema_version"] = out.get("schema_version", "cadscene_camera_track_pred_v1")
    out["meta"] = {
        **dict(out.get("meta") or {}),
        "generated_by": "cadscene.align_to_cad",
        "coordinate_system": "web_cad_world",
        "manual_keyframes_preserved": True,
        "algorithm_prediction_step": step,
    }
    return out


def _compute_metrics(correspondences: Sequence[KeyframeCorrespondence], traj: SfmTrajectory, sim3: Sim3, anchored: AnchoredAlignment, config: AlignmentConfig) -> dict:
    global_errors: list[float] = []
    anchored_errors: list[float] = []
    for corr in correspondences:
        global_state = _global_state_at_frame(corr.frame_index, traj, sim3, config)
        anchored_state = aligned_state_at_frame(corr.frame_index, traj, sim3, anchored, config)
        global_errors.append(
            float(
                np.linalg.norm(
                    [
                        corr.state.camera_x - global_state.camera_x,
                        corr.state.camera_y - global_state.camera_y,
                        corr.state.camera_z - global_state.camera_z,
                    ]
                )
            )
        )
        anchored_errors.append(
            float(
                np.linalg.norm(
                    [
                        corr.state.camera_x - anchored_state.camera_x,
                        corr.state.camera_y - anchored_state.camera_y,
                        corr.state.camera_z - anchored_state.camera_z,
                    ]
                )
            )
        )
    metrics = {
        "num_keyframes": int(len(correspondences)),
        "global_residual_m_mean": float(np.mean(global_errors)) if global_errors else 0.0,
        "global_residual_m_max": float(np.max(global_errors)) if global_errors else 0.0,
        "anchored_residual_m_mean": float(np.mean(anchored_errors)) if anchored_errors else 0.0,
        "anchored_residual_m_max": float(np.max(anchored_errors)) if anchored_errors else 0.0,
        **anchored.residual_summary(),
    }
    if len(correspondences) == 2 and anchored.position_mode != "rotation_only":
        source_baseline = (
            np.asarray(correspondences[1].center_sfm, dtype=np.float64)
            - np.asarray(correspondences[0].center_sfm, dtype=np.float64)
        )
        destination_baseline = (
            np.asarray(correspondences[1].center_cad, dtype=np.float64)
            - np.asarray(correspondences[0].center_cad, dtype=np.float64)
        )
        if (
            float(np.linalg.norm(source_baseline)) > 1e-9
            and float(np.linalg.norm(destination_baseline)) > 1e-6
        ):
            mapped_baseline = sim3.rotation @ source_baseline
            angle_rad = math.atan2(
                float(np.linalg.norm(np.cross(mapped_baseline, destination_baseline))),
                float(np.dot(mapped_baseline, destination_baseline)),
            )
            metrics["baseline_direction_error_deg"] = math.degrees(angle_rad)
    return metrics


def _alignment_json(
    sim3: Sim3,
    anchored: AnchoredAlignment,
    correspondences: Sequence[KeyframeCorrespondence],
    metrics: Mapping[str, object],
    config: AlignmentConfig,
    *,
    fov_source: str,
    intrinsics_warning: str | None,
) -> dict:
    return {
        "schema_version": "cadscene_alignment_v1",
        "alignment_mode": anchored.position_mode,
        "scale_observable": anchored.position_mode not in {
            "rotation_only",
            "metric_direct",
        },
        "transform": sim3.to_dict(),
        "sim3": sim3.to_dict(),
        "anchors": [row.to_row() for row in correspondences],
        "residuals": {
            "frames": anchored.frames.tolist(),
            "position_m": anchored.residual_positions.tolist(),
            "angle_deg": anchored.residual_angles_deg.tolist(),
        },
        "metrics": dict(metrics),
        "validation": {
            "status": "warning" if intrinsics_warning is not None else "ok",
            "fov_source": fov_source,
            "intrinsics_warning": intrinsics_warning,
            "warning": _alignment_validation_warning(
                fov_source=fov_source,
                intrinsics_warning=intrinsics_warning,
            ),
            "alignment_mode": anchored.position_mode,
            "metric_scale_locked": anchored.position_mode == "metric_direct",
        },
        "config": {
            "cad_scale": float(config.cad_scale),
            "origin_xy": [float(config.origin_xy[0]), float(config.origin_xy[1])],
            "fov": float(config.fov),
            "fov_from": config.fov_from,
            "frontend_track_step": int(config.frontend_track_step),
            "frame_step": int(config.frame_step),
            "start_frame": config.start_frame,
            "end_frame": config.end_frame,
        },
    }


def build_alignment_report(
    metrics: Mapping[str, object],
    config: AlignmentConfig,
    *,
    validation: Mapping[str, object] | None = None,
) -> str:
    validation_data = dict(validation or {})
    validation_lines: list[str] = []
    if validation_data:
        validation_lines = [
            "## Alignment validation",
            "",
            f"- status: {validation_data.get('status', 'unknown')}",
            f"- FOV source: {validation_data.get('fov_source', 'unknown')}",
        ]
        warning = validation_data.get("warning")
        if warning:
            validation_lines.append(f"- warning: {warning}")
        intrinsics_warning = validation_data.get("intrinsics_warning")
        if intrinsics_warning:
            validation_lines.append(f"- upstream detail: {intrinsics_warning}")
        validation_lines.append("")
    return "\n".join(
        [
            "# SfM-CAD 对齐报告",
            "",
            "本报告由 Stage 3A alignment core 基于 SfM trajectory、web keyframes 与 CAD 坐标配置生成。",
            "",
            "## 关键指标",
            "",
            f"- 参与对齐关键帧数量：{metrics.get('num_keyframes', 0)}",
            f"- global sim3 平均残差（米）：{float(metrics.get('global_residual_m_mean', 0.0)):.6f}",
            f"- global sim3 最大残差（米）：{float(metrics.get('global_residual_m_max', 0.0)):.6f}",
            f"- anchored path 平均残差（米）：{float(metrics.get('anchored_residual_m_mean', 0.0)):.6f}",
            f"- anchored path 最大残差（米）：{float(metrics.get('anchored_residual_m_max', 0.0)):.6f}",
            f"- 对齐模式：{metrics.get('alignment_mode', 'sfm_residual')}",
            f"- 尺度是否可观测：{'是' if metrics.get('scale_observable', True) else '否'}",
            "",
            *validation_lines,
            "## 重要说明",
            "",
            *(
                [
                    "- 当前视频被识别为纯旋转场景：人工关键帧位置没有平移基线。",
                    "- 相机位置保持人工锚点位置，姿态继续使用 SfM 旋转与人工关键帧约束。",
                    "- Sim3 scale=1.0 是不可观测情况下的协议回退值，点云相对 CAD 的绝对比例不能由该视频单独确定。",
                ]
                if metrics.get("alignment_mode") == "rotation_only"
                else ["- 当前视频具有位置基线，使用常规 SfM-CAD Sim3 与分段锚定。"]
            ),
            "",
            "## 坐标约定",
            "",
            "- SfM trajectory 保持 SfM world。",
            "- global sim3 将 SfM world 转到 CAD meters。",
            "- anchored camera path 使用 CAD meters 与 Python pitch。",
            "- camera_track_pred.json 输出为前端 web cad_world 格式，pitch 已按前后端规则反号。",
            f"- cad_scale：{float(config.cad_scale):.9g}",
            f"- origin_xy：[{float(config.origin_xy[0]):.9g}, {float(config.origin_xy[1]):.9g}]",
            "",
        ]
    )


def run_alignment(
    *,
    trajectory_path: str | Path,
    web_camera_track_path: str | Path,
    cad_dir: str | Path | None = None,
    config: AlignmentConfig,
) -> AlignmentResult:
    _ = Path(cad_dir) if cad_dir is not None else None
    traj = load_sfm_trajectory(trajectory_path)
    track = load_web_camera_track(web_camera_track_path)
    manual_fov = _manual_fov_from_track(track)
    fov_source = "trajectory"
    if manual_fov is not None:
        config = replace(config, fov=manual_fov, fov_from="config")
        fov_source = "manual"
    elif config.fov_from == "config":
        fov_source = "config"
    if config.fov_from == "config":
        _validated_configured_fov(config.fov)
    metric_direct = _is_metric_direct_trajectory(traj)
    correspondences = build_correspondences(
        track, traj, config, allow_empty=metric_direct
    )
    if metric_direct:
        sim3, anchored = _metric_direct_alignment(correspondences, traj, config)
    else:
        sim3 = estimate_global_sim3(correspondences)
        anchored = apply_segment_anchoring(sim3, correspondences, traj, config)
    path_rows = generate_aligned_camera_path(traj, sim3, anchored, config)
    camera_track_pred = generate_camera_track_pred(track, path_rows, config, fps=traj.fps)
    metrics = _compute_metrics(correspondences, traj, sim3, anchored, config)
    intrinsics_warning = _trajectory_intrinsics_warning(traj)
    _validate_alignment_result(
        metrics,
        intrinsics_warning=intrinsics_warning,
        trusted_fov=fov_source in {"manual", "config"},
    )
    return AlignmentResult(
        alignment_json=_alignment_json(
            sim3,
            anchored,
            correspondences,
            metrics,
            config,
            fov_source=fov_source,
            intrinsics_warning=intrinsics_warning,
        ),
        sfm_camera_path_rows=path_rows,
        camera_track_pred=camera_track_pred,
        keyframe_correspondences=[row.to_row() for row in correspondences],
        metrics=metrics,
    )
