from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from .models import BoundaryEvidence, MotionMode
from .shot_detection import FramePairEvidence


@dataclass(frozen=True)
class MotionAnalysisConfig:
    window_sec: float = 4.0
    step_sec: float = 1.0
    min_sustain_sec: float = 3.0
    static_merge_max_sec: float = 2.0
    mode_confidence_threshold: float = 0.55
    hysteresis_margin: float = 0.05
    static_flow_threshold_px: float = 0.6
    rotation_inlier_threshold: float = 0.65
    rotation_residual_ratio: float = 0.18
    general_inlier_threshold: float = 0.55
    general_residual_ratio: float = 0.25


@dataclass(frozen=True)
class MotionWindow:
    start_pts_sec: float
    end_pts_sec: float
    motion_mode: MotionMode
    confidence: float
    evidence_count: int
    median_flow_px: float
    median_residual_px: float
    median_homography_inlier_ratio: float


def classify_motion_window(
    evidence: list[FramePairEvidence], config: MotionAnalysisConfig | None = None
) -> MotionWindow:
    if not evidence:
        raise ValueError("motion window requires frame-pair evidence")
    settings = config or MotionAnalysisConfig()
    usable = [
        item
        for item in evidence
        if item.clarity_score >= 0.1
        and item.feature_match_count >= 8
        and item.feature_spatial_coverage >= 0.03
    ]
    start = min(item.from_pts_sec for item in evidence)
    end = max(item.to_pts_sec for item in evidence)
    if len(usable) < max(1, len(evidence) // 2):
        return MotionWindow(start, end, MotionMode.UNKNOWN, 0.35, len(evidence), 0.0, 0.0, 0.0)

    flow = float(np.median([item.flow_magnitude_px for item in usable]))
    residual = float(np.median([item.flow_residual_px for item in usable]))
    inlier_ratio = float(np.median([item.homography_inlier_ratio for item in usable]))
    residual_ratio = residual / max(flow, 0.25)
    if flow <= settings.static_flow_threshold_px:
        confidence = min(0.98, 0.82 + 0.16 * (1.0 - flow / settings.static_flow_threshold_px))
        mode = MotionMode.STATIC
    elif (
        inlier_ratio >= settings.rotation_inlier_threshold
        and residual_ratio <= settings.rotation_residual_ratio
    ):
        confidence = min(
            0.98,
            0.58
            + 0.25 * inlier_ratio
            + 0.15 * (1.0 - residual_ratio / settings.rotation_residual_ratio),
        )
        mode = MotionMode.ROTATION_DOMINANT
    elif (
        inlier_ratio <= settings.general_inlier_threshold
        or residual_ratio >= settings.general_residual_ratio
    ):
        confidence = min(
            0.95,
            0.58 + 0.2 * (1.0 - inlier_ratio) + 0.25 * min(1.0, residual_ratio),
        )
        mode = MotionMode.GENERAL_MOTION
    else:
        confidence = 0.5
        mode = MotionMode.UNKNOWN
    return MotionWindow(start, end, mode, confidence, len(evidence), flow, residual, inlier_ratio)


def build_motion_windows(
    evidence: list[FramePairEvidence], config: MotionAnalysisConfig | None = None
) -> list[MotionWindow]:
    if not evidence:
        return []
    settings = config or MotionAnalysisConfig()
    windows: list[MotionWindow] = []
    cursor = evidence[0].from_pts_sec
    final_pts = evidence[-1].to_pts_sec
    while cursor < final_pts:
        selected = [
            item
            for item in evidence
            if item.to_pts_sec > cursor and item.from_pts_sec < cursor + settings.window_sec
        ]
        if selected:
            classified = classify_motion_window(selected, settings)
            windows.append(
                replace(
                    classified,
                    start_pts_sec=cursor,
                    end_pts_sec=min(final_pts, cursor + settings.window_sec),
                )
            )
        cursor += settings.step_sec
    return windows


def _runs(windows: list[MotionWindow]) -> list[tuple[int, int, MotionMode]]:
    if not windows:
        return []
    runs: list[tuple[int, int, MotionMode]] = []
    start = 0
    for index in range(1, len(windows)):
        if windows[index].motion_mode != windows[start].motion_mode:
            runs.append((start, index, windows[start].motion_mode))
            start = index
    runs.append((start, len(windows), windows[start].motion_mode))
    return runs


def stabilize_motion_windows(
    windows: list[MotionWindow], config: MotionAnalysisConfig | None = None
) -> tuple[list[MotionWindow], list[BoundaryEvidence]]:
    if not windows:
        return [], []
    settings = config or MotionAnalysisConfig()
    stabilized = list(windows)

    for start, end, mode in _runs(stabilized):
        duration = stabilized[end - 1].end_pts_sec - stabilized[start].start_pts_sec
        if mode is not MotionMode.STATIC or duration > settings.static_merge_max_sec:
            continue
        previous = stabilized[start - 1].motion_mode if start > 0 else None
        following = stabilized[end].motion_mode if end < len(stabilized) else None
        replacement = previous if previous is not None else following
        if previous is not None and following is not None and previous == following:
            replacement = previous
        if replacement is not None:
            for index in range(start, end):
                stabilized[index] = replace(stabilized[index], motion_mode=replacement)

    runs = _runs(stabilized)
    current_mode = runs[0][2]
    boundaries: list[BoundaryEvidence] = []
    for start, end, candidate_mode in runs[1:]:
        if candidate_mode == current_mode:
            continue
        duration = stabilized[end - 1].end_pts_sec - stabilized[start].start_pts_sec
        average_confidence = float(
            np.mean([item.confidence for item in stabilized[start:end]])
        )
        threshold = settings.mode_confidence_threshold + settings.hysteresis_margin
        if duration >= settings.min_sustain_sec and average_confidence >= threshold:
            boundaries.append(
                BoundaryEvidence(
                    pts_sec=stabilized[start].start_pts_sec,
                    reasons=("motion_mode_change",),
                    confidence=average_confidence,
                )
            )
            current_mode = candidate_mode
        else:
            for index in range(start, end):
                stabilized[index] = replace(stabilized[index], motion_mode=current_mode)
    return stabilized, boundaries

