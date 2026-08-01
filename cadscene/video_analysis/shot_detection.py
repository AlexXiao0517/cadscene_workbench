from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np

from .models import BoundaryEvidence
from .pts import DecodedFrame


@dataclass(frozen=True)
class ShotDetectionConfig:
    image_change_threshold: float = 0.30
    black_luma_threshold: float = 12.0
    exposure_jump_threshold: float = 0.35
    min_feature_matches: int = 18
    min_feature_coverage: float = 0.08
    min_homography_inlier_ratio: float = 0.35
    max_flow_residual_px: float = 6.0
    pts_gap_factor: float = 3.0


@dataclass(frozen=True)
class FramePairEvidence:
    from_pts_sec: float
    to_pts_sec: float
    image_change_score: float
    black_frame_score: float
    exposure_jump_score: float
    feature_match_count: int
    feature_spatial_coverage: float
    homography_inlier_ratio: float
    flow_magnitude_px: float
    flow_residual_px: float
    clarity_score: float


def _as_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    raise ValueError(f"unsupported frame shape: {image.shape}")


def _feature_evidence(before: np.ndarray, after: np.ndarray) -> tuple[int, float, float]:
    detector = cv2.ORB_create(nfeatures=600, fastThreshold=8)
    keypoints_a, descriptors_a = detector.detectAndCompute(before, None)
    keypoints_b, descriptors_b = detector.detectAndCompute(after, None)
    if descriptors_a is None or descriptors_b is None:
        return 0, 0.0, 0.0
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(descriptors_a, descriptors_b, k=2)
    matches = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < 0.72 * pair[1].distance]
    if not matches:
        return 0, 0.0, 0.0
    points_a = np.float32([keypoints_a[match.queryIdx].pt for match in matches])
    points_b = np.float32([keypoints_b[match.trainIdx].pt for match in matches])
    height, width = before.shape

    def coverage(points: np.ndarray) -> float:
        if len(points) < 2:
            return 0.0
        span = np.ptp(points, axis=0)
        return float((span[0] * span[1]) / (width * height))

    spatial_coverage = min(coverage(points_a), coverage(points_b))
    if len(matches) < 4:
        return len(matches), spatial_coverage, 0.0
    _, mask = cv2.findHomography(points_a, points_b, cv2.RANSAC, 3.0)
    inlier_ratio = float(mask.mean()) if mask is not None else 0.0
    return len(matches), spatial_coverage, inlier_ratio


def _flow_evidence(before: np.ndarray, after: np.ndarray) -> tuple[float, float]:
    points = cv2.goodFeaturesToTrack(
        before, maxCorners=300, qualityLevel=0.01, minDistance=5, blockSize=5
    )
    diagonal = math.hypot(before.shape[1], before.shape[0])
    if points is None or len(points) < 4:
        return 0.0, diagonal
    tracked, status, _ = cv2.calcOpticalFlowPyrLK(
        before,
        after,
        points,
        None,
        winSize=(21, 21),
        maxLevel=3,
        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
    )
    if tracked is None or status is None:
        return 0.0, diagonal
    valid = status.reshape(-1).astype(bool)
    source = points.reshape(-1, 2)[valid]
    target = tracked.reshape(-1, 2)[valid]
    if len(source) < 4:
        return 0.0, diagonal
    flow_magnitude = float(np.median(np.linalg.norm(target - source, axis=1)))
    homography, mask = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
    if homography is None:
        return flow_magnitude, diagonal
    predicted = cv2.perspectiveTransform(source.reshape(-1, 1, 2), homography).reshape(-1, 2)
    residuals = np.linalg.norm(target - predicted, axis=1)
    if mask is not None and np.any(mask):
        residuals = residuals[mask.reshape(-1).astype(bool)]
    return flow_magnitude, float(np.median(residuals))


def analyze_frame_pair(before: DecodedFrame, after: DecodedFrame) -> FramePairEvidence:
    image_a = _as_gray(before.image)
    image_b = _as_gray(after.image)
    if image_a.shape != image_b.shape:
        raise ValueError("frame dimensions must match")
    image_change = float(np.mean(cv2.absdiff(image_a, image_b)) / 255.0)
    mean_a = float(np.mean(image_a))
    mean_b = float(np.mean(image_b))
    black_score = max(
        max(0.0, 1.0 - mean_a / 24.0),
        max(0.0, 1.0 - mean_b / 24.0),
    )
    exposure_jump = abs(mean_b - mean_a) / 255.0
    match_count, coverage, inlier_ratio = _feature_evidence(image_a, image_b)
    flow_magnitude, flow_residual = _flow_evidence(image_a, image_b)
    clarity = min(
        1.0,
        float(cv2.Laplacian(image_a, cv2.CV_64F).var() + cv2.Laplacian(image_b, cv2.CV_64F).var())
        / 2000.0,
    )
    return FramePairEvidence(
        from_pts_sec=before.pts_sec,
        to_pts_sec=after.pts_sec,
        image_change_score=image_change,
        black_frame_score=black_score,
        exposure_jump_score=exposure_jump,
        feature_match_count=match_count,
        feature_spatial_coverage=coverage,
        homography_inlier_ratio=inlier_ratio,
        flow_magnitude_px=flow_magnitude,
        flow_residual_px=flow_residual,
        clarity_score=clarity,
    )


def detect_shot_boundaries(
    frames: list[DecodedFrame],
    *,
    expected_interval_sec: float,
    config: ShotDetectionConfig | None = None,
    pair_evidence: list[FramePairEvidence] | None = None,
) -> list[BoundaryEvidence]:
    if expected_interval_sec <= 0:
        raise ValueError("expected_interval_sec must be positive")
    settings = config or ShotDetectionConfig()
    evidence_items = pair_evidence or [
        analyze_frame_pair(before, after) for before, after in zip(frames, frames[1:])
    ]
    if len(evidence_items) != max(0, len(frames) - 1):
        raise ValueError("pair evidence count must match adjacent frame pairs")
    boundaries: list[BoundaryEvidence] = []
    for before, after, evidence in zip(frames, frames[1:], evidence_items):
        reasons: list[str] = []
        confidence = 0.0
        if after.pts_sec <= before.pts_sec or (
            after.pts_sec - before.pts_sec > expected_interval_sec * settings.pts_gap_factor
        ):
            reasons.append("pts_or_decode_anomaly")
            confidence = max(confidence, 0.9)

        mean_before = float(np.mean(_as_gray(before.image)))
        mean_after = float(np.mean(_as_gray(after.image)))
        black_transition = (mean_before <= settings.black_luma_threshold) != (
            mean_after <= settings.black_luma_threshold
        )
        if black_transition:
            reasons.extend(("black_frame", "exposure_discontinuity"))
            confidence = max(confidence, 0.97)
        elif evidence.exposure_jump_score >= settings.exposure_jump_threshold:
            reasons.append("exposure_discontinuity")
            confidence = max(confidence, 0.85)

        weak_signals = sum(
            (
                evidence.feature_match_count < settings.min_feature_matches,
                evidence.feature_spatial_coverage < settings.min_feature_coverage,
                evidence.homography_inlier_ratio < settings.min_homography_inlier_ratio,
                evidence.flow_residual_px > settings.max_flow_residual_px,
            )
        )
        feature_failure = evidence.feature_match_count < settings.min_feature_matches
        if evidence.image_change_score >= settings.image_change_threshold and (
            feature_failure or weak_signals >= 2
        ):
            reasons.append("image_discontinuity")
            confidence = max(
                confidence,
                min(0.98, 0.52 + evidence.image_change_score * 0.6 + weak_signals * 0.04),
            )
        if reasons:
            boundaries.append(
                BoundaryEvidence(
                    pts_sec=after.pts_sec,
                    reasons=tuple(dict.fromkeys(reasons)),
                    confidence=confidence,
                )
            )
    return boundaries
