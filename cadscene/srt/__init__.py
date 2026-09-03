"""Conservative DJI SRT metadata parsing and trajectory capability detection."""

from .capability import detect_trajectory_capability
from .full_pose import (
    DJI_ABSOLUTE_NED_PROFILE,
    FullPoseBuildConfig,
    build_full_pose_trajectory,
    dji_ned_gimbal_to_cam_from_world_quat,
    horizontal_fov_intrinsics,
)
from .fixed_track_visual_pose import (
    FixedTrackPosition,
    FixedTrackVisualPoseConfig,
    OrientationSolution,
    PairRotationMeasurement,
    build_fixed_track_positions,
    estimate_video_orientations,
    interpolate_orientations,
    solve_fixed_center_rotations,
)
from .parser import analyze_srt_stream, load_srt_records

__all__ = [
    "DJI_ABSOLUTE_NED_PROFILE",
    "FullPoseBuildConfig",
    "analyze_srt_stream",
    "build_full_pose_trajectory",
    "detect_trajectory_capability",
    "dji_ned_gimbal_to_cam_from_world_quat",
    "horizontal_fov_intrinsics",
    "FixedTrackPosition",
    "FixedTrackVisualPoseConfig",
    "OrientationSolution",
    "PairRotationMeasurement",
    "build_fixed_track_positions",
    "estimate_video_orientations",
    "interpolate_orientations",
    "load_srt_records",
    "solve_fixed_center_rotations",
]
