"""Camera intrinsics and complete DJI SRT trajectory construction."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from fractions import Fraction
import json
from math import hypot, isfinite, radians, tan
import os
from pathlib import Path
import tempfile
from typing import Mapping, Sequence

from cadscene.core.camera import (
    CameraState,
    camera_to_world_rotation,
    rotation_matrix_to_quaternion_wxyz,
)
from cadscene.srt.georeference import (
    CadGeoreference,
    cad_raw_to_local_m,
    project_wgs84_to_cad_raw,
)
from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import (
    FrameSrtSample,
    FrameTimestamp,
    sample_srt_at_frames,
)


DJI_ABSOLUTE_NED_PROFILE = "dji_absolute_ned"


@dataclass(frozen=True)
class FullPoseBuildConfig:
    clip_id: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    georeference: CadGeoreference
    cad_origin_xy: tuple[float, float]
    cad_scale: float
    horizontal_fov_deg: float
    cad_z_offset_m: float = 0.0
    attitude_profile: str = DJI_ABSOLUTE_NED_PROFILE
    max_interpolation_gap_sec: float = 1.5
    minimum_registered_coverage: float = 0.8
    max_horizontal_speed_mps: float = 100.0

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("clip_id must not be empty")
        if (
            isinstance(self.source_start_pts, bool)
            or isinstance(self.source_end_pts_exclusive, bool)
            or self.source_end_pts_exclusive <= self.source_start_pts
        ):
            raise ValueError("source PTS interval must be a non-empty half-open interval")
        if self.source_time_base <= 0:
            raise ValueError("source_time_base must be positive")
        if len(self.cad_origin_xy) != 2:
            raise ValueError("cad_origin_xy must contain two values")
        finite_values = (
            *self.cad_origin_xy,
            self.cad_scale,
            self.horizontal_fov_deg,
            self.cad_z_offset_m,
            self.max_interpolation_gap_sec,
            self.minimum_registered_coverage,
            self.max_horizontal_speed_mps,
        )
        if not all(isfinite(float(value)) for value in finite_values):
            raise ValueError("full-pose numeric configuration must be finite")
        if self.cad_scale <= 0.0:
            raise ValueError("cad_scale must be positive")
        if not 1.0 < self.horizontal_fov_deg < 179.0:
            raise ValueError("horizontal_fov_deg must be inside (1, 179)")
        if self.max_interpolation_gap_sec <= 0.0:
            raise ValueError("max_interpolation_gap_sec must be positive")
        if not 0.0 < self.minimum_registered_coverage <= 1.0:
            raise ValueError("minimum_registered_coverage must be inside (0, 1]")
        if self.max_horizontal_speed_mps <= 0.0:
            raise ValueError("max_horizontal_speed_mps must be positive")
        if self.attitude_profile != DJI_ABSOLUTE_NED_PROFILE:
            raise ValueError("unsupported attitude_profile")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FullPoseBuildConfig":
        time_base = value.get("source_time_base")
        if not isinstance(time_base, Mapping):
            raise ValueError("source_time_base must be an object")
        georeference = value.get("georeference")
        if not isinstance(georeference, Mapping):
            raise ValueError("georeference must be an object")
        origin = value.get("cad_origin_xy")
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            raise ValueError("cad_origin_xy must contain two values")
        return cls(
            clip_id=str(value["clip_id"]),
            source_start_pts=int(value["source_start_pts"]),
            source_end_pts_exclusive=int(value["source_end_pts_exclusive"]),
            source_time_base=Fraction(
                int(time_base["numerator"]), int(time_base["denominator"])
            ),
            georeference=CadGeoreference.from_dict(georeference),
            cad_origin_xy=(float(origin[0]), float(origin[1])),
            cad_scale=float(value["cad_scale"]),
            horizontal_fov_deg=float(value["horizontal_fov_deg"]),
            cad_z_offset_m=float(value.get("cad_z_offset_m", 0.0)),
            attitude_profile=str(
                value.get("attitude_profile", DJI_ABSOLUTE_NED_PROFILE)
            ),
            max_interpolation_gap_sec=float(
                value.get("max_interpolation_gap_sec", 1.5)
            ),
            minimum_registered_coverage=float(
                value.get("minimum_registered_coverage", 0.8)
            ),
            max_horizontal_speed_mps=float(
                value.get("max_horizontal_speed_mps", 100.0)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "source_start_pts": self.source_start_pts,
            "source_end_pts_exclusive": self.source_end_pts_exclusive,
            "source_time_base": {
                "numerator": self.source_time_base.numerator,
                "denominator": self.source_time_base.denominator,
            },
            "georeference": self.georeference.to_dict(),
            "cad_origin_xy": list(self.cad_origin_xy),
            "cad_scale": self.cad_scale,
            "horizontal_fov_deg": self.horizontal_fov_deg,
            "cad_z_offset_m": self.cad_z_offset_m,
            "attitude_profile": self.attitude_profile,
            "max_interpolation_gap_sec": self.max_interpolation_gap_sec,
            "minimum_registered_coverage": self.minimum_registered_coverage,
            "max_horizontal_speed_mps": self.max_horizontal_speed_mps,
        }


@dataclass(frozen=True)
class FullPoseBuildResult:
    trajectory_path: Path
    camera_path_path: Path
    diagnostics_path: Path
    report_path: Path
    registered_count: int
    frame_count: int


def horizontal_fov_intrinsics(
    width: int, height: int, horizontal_fov_deg: float
) -> dict[str, object]:
    image_width = int(width)
    image_height = int(height)
    fov = float(horizontal_fov_deg)
    if image_width <= 0 or image_height <= 0:
        raise ValueError("video width and height must be positive")
    if not isfinite(fov) or not 1.0 < fov < 179.0:
        raise ValueError("horizontal_fov_deg must be finite and inside (1, 179)")
    focal = (image_width * 0.5) / tan(radians(fov) * 0.5)
    return {
        "model": "PINHOLE",
        "width": image_width,
        "height": image_height,
        "params": [
            float(focal),
            float(focal),
            image_width * 0.5,
            image_height * 0.5,
        ],
        "horizontal_fov_deg": fov,
        "fov_source": "user",
    }


def dji_ned_gimbal_to_cam_from_world_quat(
    yaw: float,
    pitch: float,
    roll: float,
    *,
    profile: str,
) -> list[float]:
    if profile != DJI_ABSOLUTE_NED_PROFILE:
        raise ValueError(
            f"unsupported DJI attitude profile: {profile!r}; "
            f"expected {DJI_ABSOLUTE_NED_PROFILE!r}"
        )
    angles = (float(yaw), float(pitch), float(roll))
    if not all(isfinite(value) for value in angles):
        raise ValueError("DJI gimbal yaw/pitch/roll must be finite")
    state = CameraState(
        yaw_deg=angles[0],
        # DJI geographic NED gimbal pitch is negative when pointing down;
        # CameraState pitch is positive when its forward axis points down.
        pitch_deg=-angles[1],
        roll_deg=angles[2],
    )
    camera_to_world = camera_to_world_rotation(state)
    return rotation_matrix_to_quaternion_wxyz(camera_to_world.T)


def _frame_map_payload(
    frame_map: Mapping[str, object] | str | Path,
) -> Mapping[str, object]:
    if isinstance(frame_map, (str, Path)):
        value = json.loads(Path(frame_map).read_text(encoding="utf-8-sig"))
    else:
        value = frame_map
    if not isinstance(value, Mapping):
        raise ValueError("frame map must be an object")
    return value


def _frame_timestamps(
    frame_map: Mapping[str, object] | str | Path,
    config: FullPoseBuildConfig,
) -> tuple[list[FrameTimestamp], list[int]]:
    payload = _frame_map_payload(frame_map)
    raw_time_base = payload.get("source_time_base")
    if not isinstance(raw_time_base, Mapping):
        raise ValueError("frame map source_time_base is missing")
    time_base = Fraction(
        int(raw_time_base["numerator"]), int(raw_time_base["denominator"])
    )
    if time_base != config.source_time_base:
        raise ValueError("frame map time base disagrees with the configured interval")
    selected: Mapping[str, object] | None = None
    raw_clips = payload.get("clips")
    if isinstance(raw_clips, list):
        selected = next(
            (
                item
                for item in raw_clips
                if isinstance(item, Mapping)
                and str(item.get("clip_id")) == config.clip_id
            ),
            None,
        )
    elif isinstance(payload.get("frames"), list):
        selected = payload
    if selected is None:
        raise ValueError("frame map does not contain the configured clip")
    start = int(selected.get("source_start_pts", config.source_start_pts))
    end = int(
        selected.get(
            "source_end_pts_exclusive", config.source_end_pts_exclusive
        )
    )
    if start != config.source_start_pts or end != config.source_end_pts_exclusive:
        raise ValueError("frame map interval disagrees with the configured interval")
    raw_frames = selected.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("frame map interval contains no frames")
    timestamps: list[FrameTimestamp] = []
    source_points: list[int] = []
    for output_ordinal, item in enumerate(raw_frames):
        if not isinstance(item, Mapping):
            raise ValueError("frame map entries must be objects")
        raw_pts = item.get("pts", item.get("source_pts"))
        if isinstance(raw_pts, bool) or not isinstance(raw_pts, int):
            raise ValueError("frame map source PTS must be integers")
        pts = int(raw_pts)
        if pts < start or pts >= end:
            raise ValueError("frame map source PTS escapes the configured interval")
        if source_points and pts <= source_points[-1]:
            raise ValueError("frame map source PTS must be strictly increasing")
        source_points.append(pts)
        timestamps.append(
            FrameTimestamp(
                source_frame_index=output_ordinal,
                extracted_index=output_ordinal,
                image_name=f"frame_{output_ordinal:06d}.png",
                pts_time_sec=float(Fraction(pts) * time_base),
                timestamp_source="source_pts",
                cfr_confirmed=False,
            )
        )
    return timestamps, source_points


def _video_metadata(value: Mapping[str, object]) -> tuple[int, int, float]:
    try:
        width = int(value["width"])
        height = int(value["height"])
        fps = float(value["fps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("video metadata requires width, height and fps") from exc
    if width <= 0 or height <= 0 or not isfinite(fps) or fps <= 0.0:
        raise ValueError("video width, height and fps must be positive")
    return width, height, fps


def _height(sample: FrameSrtSample) -> tuple[float | None, str | None]:
    if sample.rel_alt is not None and isfinite(float(sample.rel_alt)):
        return float(sample.rel_alt), "rel_alt"
    if sample.abs_alt is not None and isfinite(float(sample.abs_alt)):
        return float(sample.abs_alt), "abs_alt"
    if sample.altitude is not None and isfinite(float(sample.altitude)):
        return float(sample.altitude), "altitude"
    return None, None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def _atomic_write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise ValueError("camera path requires at least one row")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(
            handle, "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_full_pose_trajectory(
    records: Sequence[SrtRecord],
    frame_map: Mapping[str, object] | str | Path,
    video_metadata: Mapping[str, object],
    config: FullPoseBuildConfig,
    output_directory: str | Path,
) -> FullPoseBuildResult:
    if not records:
        raise ValueError("SRT contains no parseable records")
    if not config.georeference.confirmed:
        raise ValueError("CAD georeference must be confirmed")
    width, height, fps = _video_metadata(video_metadata)
    intrinsics = horizontal_fov_intrinsics(
        width, height, config.horizontal_fov_deg
    )
    timestamps, source_points = _frame_timestamps(frame_map, config)
    samples = sample_srt_at_frames(
        records,
        frame_timestamps=timestamps,
        max_interpolation_gap_sec=config.max_interpolation_gap_sec,
    )
    poses: list[dict[str, object]] = []
    path_rows: list[dict[str, object]] = []
    height_sources: dict[str, int] = {}
    registered_points: list[tuple[float, float, float]] = []
    registered_times: list[float] = []
    warnings: list[str] = []
    for frame_index, (sample, source_pts) in enumerate(
        zip(samples, source_points)
    ):
        height_value, height_source = _height(sample)
        attitude = (
            sample.gimbal_yaw,
            sample.gimbal_pitch,
            sample.gimbal_roll,
        )
        registered = bool(
            sample.gps_valid
            and height_value is not None
            and all(value is not None and isfinite(float(value)) for value in attitude)
        )
        center = [0.0, 0.0, 0.0]
        quaternion = [1.0, 0.0, 0.0, 0.0]
        projected_easting = None
        projected_northing = None
        cad_raw_x = None
        cad_raw_y = None
        yaw = None
        pitch = None
        roll = None
        if registered:
            cad_raw_x, cad_raw_y = project_wgs84_to_cad_raw(
                float(sample.longitude),
                float(sample.latitude),
                config.georeference,
            )
            if (
                config.georeference.cad_axis_mapping
                == "cad_x_easting_cad_y_northing"
            ):
                projected_easting, projected_northing = cad_raw_x, cad_raw_y
            else:
                projected_easting, projected_northing = cad_raw_y, cad_raw_x
            local_x, local_y = cad_raw_to_local_m(
                (cad_raw_x, cad_raw_y),
                config.cad_origin_xy,
                config.cad_scale,
            )
            center = [
                local_x,
                local_y,
                float(height_value) + config.cad_z_offset_m,
            ]
            yaw = float(sample.gimbal_yaw)
            pitch = -float(sample.gimbal_pitch)
            roll = float(sample.gimbal_roll)
            quaternion = dji_ned_gimbal_to_cam_from_world_quat(
                float(sample.gimbal_yaw),
                float(sample.gimbal_pitch),
                float(sample.gimbal_roll),
                profile=config.attitude_profile,
            )
            registered_points.append(tuple(center))
            registered_times.append(sample.frame_timestamp.pts_time_sec)
            height_sources[height_source or "unknown"] = (
                height_sources.get(height_source or "unknown", 0) + 1
            )
        poses.append(
            {
                "frame_index": frame_index,
                "source_pts": source_pts,
                "registered": registered,
                "center": center,
                "cam_from_world_quat_wxyz": quaternion,
                "srt_interpolated": sample.interpolated,
                "latitude": sample.latitude,
                "longitude": sample.longitude,
                "height_source": height_source,
                "projected_easting": projected_easting,
                "projected_northing": projected_northing,
                "cad_raw_x": cad_raw_x,
                "cad_raw_y": cad_raw_y,
            }
        )
        path_rows.append(
            {
                "frame_index": frame_index,
                "source_pts": source_pts,
                "camera_x": center[0],
                "camera_y": center[1],
                "camera_z": center[2],
                "yaw": yaw if yaw is not None else 0.0,
                "pitch": pitch if pitch is not None else 0.0,
                "roll": roll if roll is not None else 0.0,
                "fov": config.horizontal_fov_deg,
                "path_source": "srt_full_pose",
                "status": "ok" if registered else "unregistered",
            }
        )
    registered_count = sum(bool(pose["registered"]) for pose in poses)
    coverage = registered_count / len(poses)
    if coverage < config.minimum_registered_coverage:
        raise ValueError(
            "full-pose registered coverage is below the configured minimum: "
            f"{coverage:.3f} < {config.minimum_registered_coverage:.3f}"
        )
    speeds: list[float] = []
    for first, second, first_time, second_time in zip(
        registered_points,
        registered_points[1:],
        registered_times,
        registered_times[1:],
    ):
        elapsed = second_time - first_time
        if elapsed <= 0.0:
            raise ValueError("registered frame timestamps must be increasing")
        speeds.append(
            hypot(second[0] - first[0], second[1] - first[1]) / elapsed
        )
    max_speed = max(speeds, default=0.0)
    if max_speed > config.max_horizontal_speed_mps:
        raise ValueError(
            "SRT horizontal speed exceeds the configured safety limit: "
            f"{max_speed:.3f} m/s"
        )
    if any(source != "rel_alt" for source in height_sources):
        warnings.append(
            "Absolute or generic altitude is not tied to a verified CAD height datum."
        )
    trajectory = {
        "fps": fps,
        "width": width,
        "height": height,
        "video_width": width,
        "video_height": height,
        "intrinsics": [intrinsics],
        "poses": poses,
        "meta": {
            "trajectory_mode": "srt_full_pose",
            "coordinate_system": "cad_local_m",
            "metric_scale_locked": True,
            "horizontal_datum": "CGCS2000",
            "georeference": config.georeference.to_dict(),
            "cad_origin_xy": list(config.cad_origin_xy),
            "cad_scale": config.cad_scale,
            "height_source_counts": height_sources,
            "cad_z_offset_m": config.cad_z_offset_m,
            "attitude_profile": config.attitude_profile,
            "horizontal_fov_deg": config.horizontal_fov_deg,
            "fov_source": "user",
            "source_time_base": {
                "numerator": config.source_time_base.numerator,
                "denominator": config.source_time_base.denominator,
            },
            "source_start_pts": config.source_start_pts,
            "source_end_pts_exclusive": config.source_end_pts_exclusive,
            "warnings": warnings,
        },
    }
    diagnostics = {
        "schema_version": 1,
        "clip_id": config.clip_id,
        "frame_count": len(poses),
        "registered_count": registered_count,
        "registered_coverage": coverage,
        "max_horizontal_speed_mps": max_speed,
        "height_source_counts": height_sources,
        "warnings": warnings,
        "georeference": config.georeference.to_dict(),
    }
    report = (
        "# SRT 全姿态轨迹报告\n\n"
        f"- 总帧数：{len(poses)}\n"
        f"- 已注册帧数：{registered_count}\n"
        f"- 完整覆盖率：{coverage:.3f}\n"
        f"- 坐标系：EPSG:{config.georeference.epsg}\n"
        f"- 中央经线：{config.georeference.central_meridian_deg:g}°\n"
        f"- 水平 FOV：{config.horizontal_fov_deg:g}°（用户输入）\n"
        f"- 最大水平速度：{max_speed:.3f} m/s\n"
        "- 米制尺度：锁定为 1.0\n"
        "- 高程：相对高度变化与 CAD 高程偏移分开记录。\n"
    )
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    trajectory_path = output / "camera_trajectory_full_pose.json"
    camera_path_path = output / "camera_path_full_pose.csv"
    diagnostics_path = output / "georeference_diagnostics.json"
    report_path = output / "full_pose_report.md"
    _atomic_write_json(trajectory_path, trajectory)
    _atomic_write_csv(camera_path_path, path_rows)
    _atomic_write_json(diagnostics_path, diagnostics)
    _atomic_write_text(report_path, report)
    return FullPoseBuildResult(
        trajectory_path=trajectory_path,
        camera_path_path=camera_path_path,
        diagnostics_path=diagnostics_path,
        report_path=report_path,
        registered_count=registered_count,
        frame_count=len(poses),
    )
