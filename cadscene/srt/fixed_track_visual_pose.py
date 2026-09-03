"""SRT-constrained camera positions and visual-only attitude recovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from math import acos, degrees, hypot, isfinite
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from cadscene.srt.full_pose import _frame_timestamps
from cadscene.srt.georeference import (
    CadGeoreference,
    cad_raw_to_local_m,
    project_wgs84_to_cad_raw,
)
from cadscene.srt.schema import SrtRecord
from cadscene.srt.synchronization import sample_srt_at_frames


WORKFLOW_NAME = "srt_fixed_track_visual_pose"


@dataclass(frozen=True)
class FixedTrackVisualPoseConfig:
    clip_id: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    georeference: CadGeoreference
    cad_origin_xy: tuple[float, float]
    cad_scale: float
    horizontal_fov_deg: float
    route_offset_xyz_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    max_interpolation_gap_sec: float = 1.5
    minimum_position_coverage: float = 0.8
    keyframe_interval_sec: float = 0.5
    max_features: int = 2000
    min_pair_matches: int = 24
    max_orientation_interpolation_gap_sec: float = 2.0
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
        if len(self.route_offset_xyz_m) != 3:
            raise ValueError("route_offset_xyz_m must contain three values")
        numeric = (
            *self.cad_origin_xy,
            *self.route_offset_xyz_m,
            self.cad_scale,
            self.horizontal_fov_deg,
            self.max_interpolation_gap_sec,
            self.minimum_position_coverage,
            self.keyframe_interval_sec,
            self.max_orientation_interpolation_gap_sec,
            self.max_horizontal_speed_mps,
        )
        if not all(isfinite(float(value)) for value in numeric):
            raise ValueError("fixed-track numeric configuration must be finite")
        if self.cad_scale <= 0.0:
            raise ValueError("cad_scale must be positive")
        if not 1.0 < self.horizontal_fov_deg < 179.0:
            raise ValueError("horizontal_fov_deg must be inside (1, 179)")
        if self.max_interpolation_gap_sec <= 0.0:
            raise ValueError("max_interpolation_gap_sec must be positive")
        if not 0.0 < self.minimum_position_coverage <= 1.0:
            raise ValueError("minimum_position_coverage must be inside (0, 1]")
        if self.keyframe_interval_sec <= 0.0:
            raise ValueError("keyframe_interval_sec must be positive")
        if isinstance(self.max_features, bool) or self.max_features < 64:
            raise ValueError("max_features must be at least 64")
        if isinstance(self.min_pair_matches, bool) or self.min_pair_matches < 8:
            raise ValueError("min_pair_matches must be at least 8")
        if self.max_orientation_interpolation_gap_sec <= 0.0:
            raise ValueError("max_orientation_interpolation_gap_sec must be positive")
        if self.max_horizontal_speed_mps <= 0.0:
            raise ValueError("max_horizontal_speed_mps must be positive")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FixedTrackVisualPoseConfig":
        time_base = value.get("source_time_base")
        georeference = value.get("georeference")
        origin = value.get("cad_origin_xy")
        offset = value.get("route_offset_xyz_m", (0.0, 0.0, 0.0))
        if not isinstance(time_base, Mapping):
            raise ValueError("source_time_base must be an object")
        if not isinstance(georeference, Mapping):
            raise ValueError("georeference must be an object")
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            raise ValueError("cad_origin_xy must contain two values")
        if not isinstance(offset, (list, tuple)) or len(offset) != 3:
            raise ValueError("route_offset_xyz_m must contain three values")
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
            route_offset_xyz_m=(
                float(offset[0]),
                float(offset[1]),
                float(offset[2]),
            ),
            max_interpolation_gap_sec=float(
                value.get("max_interpolation_gap_sec", 1.5)
            ),
            minimum_position_coverage=float(
                value.get("minimum_position_coverage", 0.8)
            ),
            keyframe_interval_sec=float(value.get("keyframe_interval_sec", 0.5)),
            max_features=int(value.get("max_features", 2000)),
            min_pair_matches=int(value.get("min_pair_matches", 24)),
            max_orientation_interpolation_gap_sec=float(
                value.get("max_orientation_interpolation_gap_sec", 2.0)
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
            "route_offset_xyz_m": list(self.route_offset_xyz_m),
            "max_interpolation_gap_sec": self.max_interpolation_gap_sec,
            "minimum_position_coverage": self.minimum_position_coverage,
            "keyframe_interval_sec": self.keyframe_interval_sec,
            "max_features": self.max_features,
            "min_pair_matches": self.min_pair_matches,
            "max_orientation_interpolation_gap_sec": (
                self.max_orientation_interpolation_gap_sec
            ),
            "max_horizontal_speed_mps": self.max_horizontal_speed_mps,
        }


@dataclass(frozen=True)
class FixedTrackPosition:
    frame_index: int
    source_pts: int
    pts_time_sec: float
    canonical_center: tuple[float, float, float]
    center: tuple[float, float, float]
    latitude: float
    longitude: float
    rel_alt: float
    abs_alt: float | None
    projected_easting: float
    projected_northing: float
    cad_raw_x: float
    cad_raw_y: float
    interpolated: bool
    source_entry_before: int | None
    source_entry_after: int | None
    height_source: str = "rel_alt"


@dataclass(frozen=True)
class PairRotationMeasurement:
    first_frame: int
    second_frame: int
    rotation_second_from_first: np.ndarray
    translation_direction_second: np.ndarray
    inlier_count: int


@dataclass(frozen=True)
class OrientationSolution:
    status: str
    rotations: Mapping[int, np.ndarray]
    relative_rotations: Mapping[int, np.ndarray] = field(default_factory=dict)
    component_ids: Mapping[int, int] = field(default_factory=dict)
    recommended_anchor_frame: int | None = None
    diagnostics: tuple[Mapping[str, object], ...] = ()
    warnings: tuple[str, ...] = ()


def _validated_rotation(value: np.ndarray, field: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{field} must be a finite 3x3 rotation matrix")
    if not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-5):
        raise ValueError(f"{field} must be orthonormal")
    if not np.isclose(np.linalg.det(matrix), 1.0, atol=1e-5):
        raise ValueError(f"{field} determinant must be one")
    return matrix


def _measurement_components(
    measurements: Sequence[PairRotationMeasurement],
) -> tuple[tuple[set[int], dict[int, np.ndarray]], ...]:
    adjacency: dict[int, list[tuple[int, np.ndarray]]] = {}
    for item in measurements:
        if item.first_frame == item.second_frame:
            raise ValueError("pair rotation measurement frames must be distinct")
        relative = _validated_rotation(
            item.rotation_second_from_first,
            "rotation_second_from_first",
        )
        adjacency.setdefault(item.first_frame, []).append(
            (item.second_frame, relative)
        )
        adjacency.setdefault(item.second_frame, []).append(
            (item.first_frame, relative.T)
        )
    components: list[tuple[set[int], dict[int, np.ndarray]]] = []
    unseen = set(adjacency)
    while unseen:
        root = min(unseen)
        transforms = {root: np.eye(3, dtype=np.float64)}
        pending = [root]
        component: set[int] = set()
        while pending:
            frame = pending.pop()
            if frame in component:
                continue
            component.add(frame)
            unseen.discard(frame)
            for neighbor, relative in adjacency.get(frame, ()):
                candidate = relative @ transforms[frame]
                if neighbor not in transforms:
                    transforms[neighbor] = candidate
                    pending.append(neighbor)
        components.append((component, transforms))
    return tuple(components)


def _component_world_anchor(
    component: set[int],
    transforms: Mapping[int, np.ndarray],
    centers: Mapping[int, np.ndarray],
    measurements: Sequence[PairRotationMeasurement],
) -> tuple[np.ndarray | None, list[dict[str, object]], str | None]:
    world_directions: list[np.ndarray] = []
    root_camera_directions: list[np.ndarray] = []
    weights: list[float] = []
    diagnostics: list[dict[str, object]] = []
    for item in measurements:
        if item.first_frame not in component or item.second_frame not in component:
            continue
        if item.first_frame not in centers or item.second_frame not in centers:
            continue
        baseline = np.asarray(centers[item.first_frame], dtype=np.float64) - np.asarray(
            centers[item.second_frame], dtype=np.float64
        )
        length = float(np.linalg.norm(baseline))
        direction_second = np.asarray(
            item.translation_direction_second, dtype=np.float64
        ).reshape(-1)
        direction_norm = float(np.linalg.norm(direction_second))
        if (
            baseline.shape != (3,)
            or not np.all(np.isfinite(baseline))
            or direction_second.shape != (3,)
            or not np.all(np.isfinite(direction_second))
            or length <= 1e-6
            or direction_norm <= 1e-9
        ):
            continue
        world_direction = baseline / length
        root_direction = (
            transforms[item.second_frame].T
            @ (direction_second / direction_norm)
        )
        root_direction /= np.linalg.norm(root_direction)
        world_directions.append(world_direction)
        root_camera_directions.append(root_direction)
        weights.append(float(max(1, int(item.inlier_count))))
        diagnostics.append(
            {
                "first_frame": item.first_frame,
                "second_frame": item.second_frame,
                "baseline_m": length,
                "inlier_count": int(item.inlier_count),
            }
        )
    if len(world_directions) < 2:
        return None, diagnostics, "fixed-center world attitude is unobservable from fewer than two baselines"
    source = np.asarray(world_directions, dtype=np.float64)
    singular_values = np.linalg.svd(source, compute_uv=False)
    ratio = (
        float(singular_values[1] / singular_values[0])
        if singular_values[0] > 1e-12
        else 0.0
    )
    if ratio < 0.02:
        return None, diagnostics, "fixed-center world attitude is unobservable on a straight track"
    target = np.asarray(root_camera_directions, dtype=np.float64)
    aligned, rms = Rotation.align_vectors(
        target,
        source,
        weights=np.asarray(weights, dtype=np.float64),
    )
    root_rotation = aligned.as_matrix()
    residuals: list[float] = []
    for expected, observed in zip(target, source @ root_rotation.T):
        cosine = float(np.clip(np.dot(expected, observed), -1.0, 1.0))
        residuals.append(degrees(acos(cosine)))
    if residuals and max(residuals) > 30.0:
        return None, diagnostics, "fixed-center translation directions are inconsistent"
    for row, residual in zip(diagnostics, residuals):
        row["direction_residual_deg"] = residual
        row["anchor_rms"] = float(rms)
        row["baseline_direction_ratio"] = ratio
    return root_rotation, diagnostics, None


def solve_fixed_center_rotations(
    centers: Mapping[int, np.ndarray | Sequence[float]],
    measurements: Sequence[PairRotationMeasurement],
) -> OrientationSolution:
    """Recover camera rotations while treating every supplied center as immutable."""

    fixed_centers: dict[int, np.ndarray] = {}
    for frame, value in centers.items():
        center = np.asarray(value, dtype=np.float64)
        if center.shape != (3,) or not np.all(np.isfinite(center)):
            raise ValueError("camera centers must be finite XYZ vectors")
        fixed_centers[int(frame)] = center.copy()
    if len(fixed_centers) < 2:
        return OrientationSolution(
            status="position_only",
            rotations={},
            warnings=("fixed-center world attitude is unobservable without a route",),
        )
    rotations: dict[int, np.ndarray] = {}
    relative_rotations: dict[int, np.ndarray] = {}
    component_ids: dict[int, int] = {}
    diagnostics: list[Mapping[str, object]] = []
    warnings: list[str] = []
    components = sorted(
        _measurement_components(measurements),
        key=lambda item: min(item[0]),
    )
    for component_id, (component, transforms) in enumerate(components):
        for frame in sorted(component):
            if frame not in fixed_centers:
                continue
            relative_rotations[frame] = _validated_rotation(
                transforms[frame],
                "relative camera rotation",
            )
            component_ids[frame] = component_id
        root_rotation, rows, warning = _component_world_anchor(
            component,
            transforms,
            fixed_centers,
            measurements,
        )
        diagnostics.extend(rows)
        if root_rotation is None:
            if warning:
                warnings.append(warning)
            continue
        for frame in component:
            if frame not in fixed_centers:
                continue
            rotations[frame] = _validated_rotation(
                transforms[frame] @ root_rotation,
                "solved camera rotation",
            )
    coverage = len(rotations) / len(fixed_centers)
    status = (
        "orientation_ready"
        if coverage >= 0.8
        else "orientation_partial"
        if rotations
        else "position_only"
    )
    if not measurements:
        warnings.append("fixed-center world attitude is unobservable without visual pairs")
    recommended_anchor_frame: int | None = None
    available_components = [
        (component_id, {frame for frame in component if frame in fixed_centers})
        for component_id, (component, _transforms) in enumerate(components)
        if any(frame in fixed_centers for frame in component)
    ]
    if available_components:
        selected_component_id, selected_component = max(
            available_components,
            key=lambda item: (len(item[1]), -min(item[1])),
        )
        support = {frame: 0 for frame in selected_component}
        degree = {frame: 0 for frame in selected_component}
        for measurement in measurements:
            if (
                measurement.first_frame in selected_component
                and measurement.second_frame in selected_component
            ):
                weight = max(1, int(measurement.inlier_count))
                support[measurement.first_frame] += weight
                support[measurement.second_frame] += weight
                degree[measurement.first_frame] += 1
                degree[measurement.second_frame] += 1
        internal = [frame for frame in selected_component if degree[frame] >= 2]
        candidates = internal or list(selected_component)
        recommended_anchor_frame = max(
            candidates,
            key=lambda frame: (support[frame], -frame),
        )
        if component_ids.get(recommended_anchor_frame) != selected_component_id:
            raise RuntimeError("recommended anchor component is inconsistent")
    return OrientationSolution(
        status=status,
        rotations=rotations,
        relative_rotations=relative_rotations,
        component_ids=component_ids,
        recommended_anchor_frame=recommended_anchor_frame,
        diagnostics=tuple(diagnostics),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _interpolate_relative_orientations(
    rotations: Mapping[int, np.ndarray],
    component_ids: Mapping[int, int],
    frame_times: Mapping[int, float],
    *,
    max_gap_sec: float,
) -> tuple[dict[int, np.ndarray], dict[int, int]]:
    published: dict[int, np.ndarray] = {}
    published_components: dict[int, int] = {}
    for component_id in sorted(set(component_ids.values())):
        component_rotations = {
            frame: rotation
            for frame, rotation in rotations.items()
            if component_ids.get(frame) == component_id
        }
        if not component_rotations:
            continue
        first = min(component_rotations)
        last = max(component_rotations)
        component_times = {
            frame: value
            for frame, value in frame_times.items()
            if first <= frame <= last
        }
        interpolated = interpolate_orientations(
            component_rotations,
            component_times,
            max_gap_sec=max_gap_sec,
        )
        for frame, rotation in interpolated.items():
            if rotation is None:
                continue
            published[frame] = rotation
            published_components[frame] = component_id
    return published, published_components


def interpolate_orientations(
    rotations: Mapping[int, np.ndarray],
    frame_times: Mapping[int, float],
    *,
    max_gap_sec: float,
) -> dict[int, np.ndarray | None]:
    """SLERP solved rotations without crossing unsupported time gaps."""

    if not isfinite(float(max_gap_sec)) or max_gap_sec <= 0.0:
        raise ValueError("max_gap_sec must be finite and positive")
    ordered_frames = sorted(int(frame) for frame in frame_times)
    result: dict[int, np.ndarray | None] = {frame: None for frame in ordered_frames}
    known = sorted(
        frame
        for frame in rotations
        if frame in frame_times
        and isfinite(float(frame_times[frame]))
    )
    for frame in known:
        result[frame] = _validated_rotation(rotations[frame], "orientation")
    for first, second in zip(known, known[1:]):
        first_time = float(frame_times[first])
        second_time = float(frame_times[second])
        duration = second_time - first_time
        if duration <= 0.0 or duration > float(max_gap_sec):
            continue
        interpolator = Slerp(
            [first_time, second_time],
            Rotation.from_matrix(
                np.asarray([rotations[first], rotations[second]], dtype=np.float64)
            ),
        )
        for frame in ordered_frames:
            time = float(frame_times[frame])
            if first_time < time < second_time:
                result[frame] = interpolator([time]).as_matrix()[0]
    return result


def _pinhole_camera_matrix(intrinsics: Mapping[str, object]) -> np.ndarray:
    if str(intrinsics.get("model", "")).upper() != "PINHOLE":
        raise ValueError("visual attitude estimation requires PINHOLE intrinsics")
    params = intrinsics.get("params")
    if not isinstance(params, (list, tuple)) or len(params) != 4:
        raise ValueError("PINHOLE intrinsics require fx, fy, cx and cy")
    fx, fy, cx, cy = (float(value) for value in params)
    if not all(isfinite(value) for value in (fx, fy, cx, cy)) or fx <= 0 or fy <= 0:
        raise ValueError("PINHOLE intrinsics must be finite with positive focal lengths")
    return np.asarray(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _prepare_visual_frame(
    frame: np.ndarray,
    camera_matrix: np.ndarray,
    *,
    intrinsics_width: int,
    intrinsics_height: int,
    maximum_width: int = 960,
) -> tuple[np.ndarray, np.ndarray]:
    import cv2

    image = np.asarray(frame)
    if image.ndim not in (2, 3) or image.shape[0] <= 0 or image.shape[1] <= 0:
        raise ValueError("decoded video frame is empty")
    source_height, source_width = image.shape[:2]
    scale = min(1.0, float(maximum_width) / float(source_width))
    output_width = max(1, int(round(source_width * scale)))
    output_height = max(1, int(round(source_height * scale)))
    if (output_width, output_height) != (source_width, source_height):
        image = cv2.resize(image, (output_width, output_height))
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scaled = np.asarray(camera_matrix, dtype=np.float64).copy()
    scaled[0, :] *= output_width / float(intrinsics_width)
    scaled[1, :] *= output_height / float(intrinsics_height)
    scaled[2, :] = [0.0, 0.0, 1.0]
    return image, scaled


def _extract_pair_rotation_measurement(
    first_frame: int,
    second_frame: int,
    first_image: np.ndarray,
    second_image: np.ndarray,
    camera_matrix: np.ndarray,
    *,
    max_features: int,
    min_pair_matches: int,
) -> tuple[PairRotationMeasurement | None, dict[str, object]]:
    import cv2

    diagnostic: dict[str, object] = {
        "first_frame": int(first_frame),
        "second_frame": int(second_frame),
        "status": "rejected",
        "feature_count_first": 0,
        "feature_count_second": 0,
        "ratio_test_matches": 0,
        "pose_inliers": 0,
    }
    detector = cv2.ORB_create(nfeatures=int(max_features))
    first_keypoints, first_descriptors = detector.detectAndCompute(first_image, None)
    second_keypoints, second_descriptors = detector.detectAndCompute(second_image, None)
    diagnostic["feature_count_first"] = len(first_keypoints or ())
    diagnostic["feature_count_second"] = len(second_keypoints or ())
    if first_descriptors is None or second_descriptors is None:
        diagnostic["reason"] = "descriptors_unavailable"
        return None, diagnostic
    candidates = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(
        first_descriptors,
        second_descriptors,
        k=2,
    )
    matches = [
        first
        for pair in candidates
        if len(pair) == 2
        for first, second in [pair]
        if first.distance < 0.75 * second.distance
    ]
    diagnostic["ratio_test_matches"] = len(matches)
    if len(matches) < int(min_pair_matches):
        diagnostic["reason"] = "insufficient_matches"
        return None, diagnostic
    first_points = np.asarray(
        [first_keypoints[item.queryIdx].pt for item in matches], dtype=np.float64
    )
    second_points = np.asarray(
        [second_keypoints[item.trainIdx].pt for item in matches], dtype=np.float64
    )
    essential, mask = cv2.findEssentialMat(
        first_points,
        second_points,
        camera_matrix,
        method=cv2.RANSAC,
        prob=0.999,
        threshold=1.0,
    )
    if essential is None:
        diagnostic["reason"] = "essential_matrix_unavailable"
        return None, diagnostic
    essential = np.asarray(essential, dtype=np.float64)
    if essential.shape != (3, 3):
        essential = essential[:3, :3]
    inliers, relative, translation, _pose_mask = cv2.recoverPose(
        essential,
        first_points,
        second_points,
        camera_matrix,
        mask=mask,
    )
    diagnostic["pose_inliers"] = int(inliers)
    if int(inliers) < 15:
        diagnostic["reason"] = "insufficient_pose_inliers"
        return None, diagnostic
    measurement = PairRotationMeasurement(
        first_frame=int(first_frame),
        second_frame=int(second_frame),
        rotation_second_from_first=_validated_rotation(
            np.asarray(relative, dtype=np.float64),
            "recovered relative rotation",
        ),
        translation_direction_second=np.asarray(translation, dtype=np.float64).reshape(3),
        inlier_count=int(inliers),
    )
    diagnostic["status"] = "accepted"
    return measurement, diagnostic


def _sample_visual_keyframes(
    positions: Sequence[FixedTrackPosition], interval_sec: float
) -> tuple[FixedTrackPosition, ...]:
    ordered = sorted(positions, key=lambda item: item.frame_index)
    if not ordered:
        return ()
    selected = [ordered[0]]
    for position in ordered[1:]:
        if position.pts_time_sec - selected[-1].pts_time_sec >= interval_sec - 1e-9:
            selected.append(position)
    if selected[-1].frame_index != ordered[-1].frame_index:
        selected.append(ordered[-1])
    return tuple(selected)


def estimate_video_orientations(
    video_path: str | Path,
    positions: Sequence[FixedTrackPosition],
    intrinsics: Mapping[str, object],
    config: FixedTrackVisualPoseConfig,
    *,
    progress: Callable[[float, str], None] | None = None,
) -> OrientationSolution:
    """Estimate attitudes from video while preserving the supplied SRT→CAD centers."""

    import cv2

    if not positions:
        return OrientationSolution(
            status="position_only",
            rotations={},
            warnings=("fixed-center world attitude is unobservable without positions",),
        )
    camera_matrix = _pinhole_camera_matrix(intrinsics)
    try:
        intrinsics_width = int(intrinsics["width"])
        intrinsics_height = int(intrinsics["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("intrinsics require positive width and height") from exc
    if intrinsics_width <= 0 or intrinsics_height <= 0:
        raise ValueError("intrinsics require positive width and height")
    selected = _sample_visual_keyframes(positions, config.keyframe_interval_sec)
    target_frames = {position.frame_index for position in selected}
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"cannot open video for visual attitude estimation: {video_path}")
    decoded: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    warnings: list[str] = []
    maximum_target = max(target_frames)
    try:
        frame_index = 0
        while frame_index <= maximum_target:
            ok, frame = capture.read()
            if not ok:
                warnings.append(
                    f"video decode ended before fixed-track frame {maximum_target}"
                )
                break
            if frame_index in target_frames:
                decoded[frame_index] = _prepare_visual_frame(
                    frame,
                    camera_matrix,
                    intrinsics_width=intrinsics_width,
                    intrinsics_height=intrinsics_height,
                )
                if progress is not None:
                    progress(
                        len(decoded) / max(1, len(target_frames)),
                        f"正在提取视觉关键帧 {len(decoded)}/{len(target_frames)}",
                    )
            frame_index += 1
    finally:
        capture.release()
    measurements: list[PairRotationMeasurement] = []
    extraction_diagnostics: list[Mapping[str, object]] = []
    for first, second in zip(selected, selected[1:]):
        first_decoded = decoded.get(first.frame_index)
        second_decoded = decoded.get(second.frame_index)
        if first_decoded is None or second_decoded is None:
            extraction_diagnostics.append(
                {
                    "first_frame": first.frame_index,
                    "second_frame": second.frame_index,
                    "status": "rejected",
                    "reason": "frame_decode_unavailable",
                }
            )
            continue
        first_image, first_camera = first_decoded
        second_image, second_camera = second_decoded
        if first_image.shape != second_image.shape or not np.allclose(
            first_camera, second_camera
        ):
            extraction_diagnostics.append(
                {
                    "first_frame": first.frame_index,
                    "second_frame": second.frame_index,
                    "status": "rejected",
                    "reason": "frame_geometry_changed",
                }
            )
            continue
        measurement, diagnostic = _extract_pair_rotation_measurement(
            first.frame_index,
            second.frame_index,
            first_image,
            second_image,
            first_camera,
            max_features=config.max_features,
            min_pair_matches=config.min_pair_matches,
        )
        extraction_diagnostics.append(diagnostic)
        if measurement is not None:
            measurements.append(measurement)
    centers = {
        position.frame_index: np.asarray(position.center, dtype=np.float64).copy()
        for position in positions
    }
    solved = solve_fixed_center_rotations(centers, measurements)
    times = {position.frame_index: position.pts_time_sec for position in positions}
    interpolated = interpolate_orientations(
        solved.rotations,
        times,
        max_gap_sec=config.max_orientation_interpolation_gap_sec,
    )
    published = {
        frame: rotation
        for frame, rotation in interpolated.items()
        if rotation is not None
    }
    relative_published, relative_components = _interpolate_relative_orientations(
        solved.relative_rotations,
        solved.component_ids,
        times,
        max_gap_sec=config.max_orientation_interpolation_gap_sec,
    )
    coverage = len(published) / len(positions)
    status = (
        "orientation_ready"
        if coverage >= 0.8
        else "orientation_partial"
        if published
        else "position_only"
    )
    return OrientationSolution(
        status=status,
        rotations=published,
        relative_rotations=relative_published,
        component_ids=relative_components,
        recommended_anchor_frame=solved.recommended_anchor_frame,
        diagnostics=tuple(solved.diagnostics) + tuple(extraction_diagnostics),
        warnings=tuple(dict.fromkeys((*solved.warnings, *warnings))),
    )


def build_fixed_track_positions(
    records: Sequence[SrtRecord],
    frame_map: Mapping[str, object] | str,
    config: FixedTrackVisualPoseConfig,
) -> tuple[FixedTrackPosition, ...]:
    """Project valid SRT samples into CAD coordinates without visual position edits."""

    if not records:
        raise ValueError("SRT contains no parseable records")
    if not config.georeference.confirmed:
        raise ValueError("CAD georeference must be confirmed")
    timestamps, source_points = _frame_timestamps(frame_map, config)  # type: ignore[arg-type]
    samples = sample_srt_at_frames(
        records,
        frame_timestamps=timestamps,
        max_interpolation_gap_sec=config.max_interpolation_gap_sec,
    )
    valid_relative_height = sum(
        sample.rel_alt is not None and isfinite(float(sample.rel_alt))
        for sample in samples
    )
    if valid_relative_height == 0:
        raise ValueError(
            "SRT relative height is required; absolute height is diagnostic-only"
        )

    offset = tuple(float(value) for value in config.route_offset_xyz_m)
    positions: list[FixedTrackPosition] = []
    for frame_index, (sample, source_pts) in enumerate(zip(samples, source_points)):
        if not sample.gps_valid or sample.rel_alt is None:
            continue
        values = (
            sample.latitude,
            sample.longitude,
            sample.rel_alt,
        )
        if any(value is None or not isfinite(float(value)) for value in values):
            continue
        latitude = float(sample.latitude)
        longitude = float(sample.longitude)
        rel_alt = float(sample.rel_alt)
        cad_raw_x, cad_raw_y = project_wgs84_to_cad_raw(
            longitude, latitude, config.georeference
        )
        if (
            config.georeference.cad_axis_mapping
            == "cad_x_easting_cad_y_northing"
        ):
            easting, northing = cad_raw_x, cad_raw_y
        else:
            easting, northing = cad_raw_y, cad_raw_x
        local_x, local_y = cad_raw_to_local_m(
            (cad_raw_x, cad_raw_y), config.cad_origin_xy, config.cad_scale
        )
        canonical = (float(local_x), float(local_y), rel_alt)
        center = tuple(canonical[index] + offset[index] for index in range(3))
        abs_alt = (
            float(sample.abs_alt)
            if sample.abs_alt is not None and isfinite(float(sample.abs_alt))
            else None
        )
        positions.append(
            FixedTrackPosition(
                frame_index=frame_index,
                source_pts=int(source_pts),
                pts_time_sec=float(sample.frame_timestamp.pts_time_sec),
                canonical_center=canonical,
                center=center,
                latitude=latitude,
                longitude=longitude,
                rel_alt=rel_alt,
                abs_alt=abs_alt,
                projected_easting=float(easting),
                projected_northing=float(northing),
                cad_raw_x=float(cad_raw_x),
                cad_raw_y=float(cad_raw_y),
                interpolated=bool(sample.interpolated),
                source_entry_before=sample.source_entry_before,
                source_entry_after=sample.source_entry_after,
            )
        )

    coverage = len(positions) / len(timestamps)
    if coverage < config.minimum_position_coverage:
        raise ValueError(
            "fixed-track position coverage is below the configured minimum: "
            f"{coverage:.3f} < {config.minimum_position_coverage:.3f}"
        )
    max_speed = 0.0
    for first, second in zip(positions, positions[1:]):
        elapsed = second.pts_time_sec - first.pts_time_sec
        if elapsed <= 0.0:
            raise ValueError("fixed-track frame timestamps must be increasing")
        speed = hypot(
            second.canonical_center[0] - first.canonical_center[0],
            second.canonical_center[1] - first.canonical_center[1],
        ) / elapsed
        max_speed = max(max_speed, speed)
    if max_speed > config.max_horizontal_speed_mps:
        raise ValueError(
            "SRT horizontal speed exceeds the configured safety limit: "
            f"{max_speed:.3f} m/s"
        )
    return tuple(positions)
