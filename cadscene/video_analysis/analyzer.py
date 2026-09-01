from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
from statistics import fmean, median
import time
from typing import Any, Callable

from cadscene.srt.parser import analyze_srt_stream

from .artifacts import VIDEO_ANALYSIS_DIRECTORY, publish_analysis_revision
from .models import BoundaryEvidence, LogicalClip, MotionMode, PtsMapping
from .motion import (
    MotionAnalysisConfig,
    MotionWindow,
    build_motion_windows,
    stabilize_motion_windows,
)
from .pts import (
    decode_indexed_sparse_frames,
    decode_sparse_frame_ranges,
    probe_video_pts,
)
from .recommendation import assess_clip_srt_coverage, recommend_workflow
from .segmentation import CutCandidate, SegmentationConfig, plan_clip_intervals
from .shot_detection import (
    FramePairEvidence,
    analyze_frame_pair,
    coalesce_boundaries,
    detect_shot_boundaries,
    verify_candidate_boundaries,
)


def _new_revision() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"analysis-{timestamp}"


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _dominant_motion(
    windows: list[MotionWindow], start_pts_sec: float, end_pts_sec: float
) -> tuple[MotionMode, float]:
    scores: dict[MotionMode, float] = {}
    confidence_weight: dict[MotionMode, float] = {}
    for window in windows:
        overlap = max(
            0.0,
            min(end_pts_sec, window.end_pts_sec) - max(start_pts_sec, window.start_pts_sec),
        )
        if overlap <= 0:
            continue
        scores[window.motion_mode] = scores.get(window.motion_mode, 0.0) + overlap
        confidence_weight[window.motion_mode] = confidence_weight.get(
            window.motion_mode, 0.0
        ) + overlap * window.confidence
    if not scores:
        return MotionMode.UNKNOWN, 0.3
    mode = max(scores, key=scores.get)
    return mode, confidence_weight[mode] / scores[mode]


def _mandatory_boundaries_for_source(
    boundaries: list[BoundaryEvidence],
    *,
    source_start_pts_sec: float,
    source_end_pts_exclusive_sec: float,
    hard_max_sec: float = 60.0,
) -> list[BoundaryEvidence]:
    """Keep an entire short upload as one logical clip.

    Shot evidence remains in ``detected_boundaries.json`` for diagnostics.  It
    only stops being a mandatory logical cut when the complete source already
    satisfies the strict duration limit.
    """
    duration = source_end_pts_exclusive_sec - source_start_pts_sec
    if duration < hard_max_sec:
        return []
    return list(boundaries)


def _rotation_evidence_verified(
    windows: list[MotionWindow],
    *,
    source_start_pts_sec: float,
    source_end_pts_exclusive_sec: float,
) -> bool:
    """Conservatively confirm a short, sustained rotation-only source."""
    if source_end_pts_exclusive_sec - source_start_pts_sec >= 60.0:
        return False
    selected = [
        window
        for window in windows
        if window.end_pts_sec > source_start_pts_sec
        and window.start_pts_sec < source_end_pts_exclusive_sec
    ]
    rotation = [
        window
        for window in selected
        if window.motion_mode is MotionMode.ROTATION_DOMINANT
    ]
    contradictory = [
        window
        for window in selected
        if window.motion_mode in {MotionMode.GENERAL_MOTION, MotionMode.UNKNOWN}
    ]
    if len(rotation) < 3 or contradictory:
        return False
    if len(rotation) / len(selected) < 0.6:
        return False
    return (
        fmean(window.confidence for window in rotation) >= 0.85
        and median(window.median_homography_inlier_ratio for window in rotation)
        >= 0.85
        and median(window.median_residual_px for window in rotation) <= 1.5
    )


def _analysis_range(
    sampled_pts: list[float], start_pts_sec: float, end_pts_sec: float
) -> tuple[float, float]:
    pts = [pts for pts in sampled_pts if start_pts_sec <= pts < end_pts_sec]
    if not pts:
        return start_pts_sec, end_pts_sec
    return min(pts), max(pts)


def _scene_boundaries_for_segmentation(
    boundaries: list[BoundaryEvidence],
    *,
    source_end_pts_sec: float,
    recent_frame_lumas: list[float],
    terminal_guard_sec: float,
    source_start_pts_sec: float = 0.0,
    opening_guard_sec: float = 0.0,
) -> list[BoundaryEvidence]:
    """Keep terminal fade evidence without creating a meaningless black tail."""
    if terminal_guard_sec < 0 or opening_guard_sec < 0:
        raise ValueError("boundary guard durations must be non-negative")
    tail = recent_frame_lumas[-3:]
    # A dark final sample alone may be a genuine hard cut. Suppress a terminal
    # boundary only when multiple samples establish a monotonic fade-to-black.
    terminal_fade = (
        len(tail) == 3
        and tail[0] > tail[1] > tail[2]
        and tail[2] <= 24.0
        and tail[0] - tail[2] >= 12.0
    )
    retained = [
        boundary
        for boundary in boundaries
        if not (
            (
                terminal_fade
                and 0.0 <= source_end_pts_sec - boundary.pts_sec <= terminal_guard_sec
            )
            or (
                "black_frame" in boundary.reasons
                and 0.0
                <= boundary.pts_sec - source_start_pts_sec
                <= opening_guard_sec
            )
        )
    ]
    normalized: list[BoundaryEvidence] = []
    index = 0
    while index < len(retained):
        boundary = retained[index]
        if "black_frame" not in boundary.reasons:
            normalized.append(boundary)
            index += 1
            continue
        run = [boundary]
        index += 1
        while index < len(retained) and "black_frame" in retained[index].reasons:
            run.append(retained[index])
            index += 1
        normalized.append(
            BoundaryEvidence(
                run[-1].pts_sec,
                tuple(dict.fromkeys(reason for item in run for reason in item.reasons)),
                max(item.confidence for item in run),
            )
        )
    return normalized


def _candidate_verification_ranges(
    boundaries: list[BoundaryEvidence],
    pair_evidence: list[FramePairEvidence],
    *,
    margin_sec: float,
) -> list[tuple[float, float]]:
    if margin_sec < 0:
        raise ValueError("margin_sec must be non-negative")
    candidate_pts = [boundary.pts_sec for boundary in boundaries]
    return [
        (evidence.from_pts_sec - margin_sec, evidence.to_pts_sec + margin_sec)
        for evidence in pair_evidence
        if any(abs(evidence.to_pts_sec - pts_sec) <= 1e-6 for pts_sec in candidate_pts)
    ]


def _windows_csv(windows: list[MotionWindow]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=(
            "start_pts_sec",
            "end_pts_sec",
            "motion_mode",
            "confidence",
            "evidence_count",
            "median_flow_px",
            "median_residual_px",
            "median_homography_inlier_ratio",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    for window in windows:
        writer.writerow(
            {
                "start_pts_sec": window.start_pts_sec,
                "end_pts_sec": window.end_pts_sec,
                "motion_mode": window.motion_mode.value,
                "confidence": window.confidence,
                "evidence_count": window.evidence_count,
                "median_flow_px": window.median_flow_px,
                "median_residual_px": window.median_residual_px,
                "median_homography_inlier_ratio": window.median_homography_inlier_ratio,
            }
        )
    return stream.getvalue()


def _report(
    *,
    project_id: str,
    revision: str,
    video_path: Path,
    elapsed_sec: float,
    clips: list[dict[str, Any]],
    boundary_count: int,
) -> str:
    lines = [
        "# Video analysis report",
        "",
        f"- Project: {project_id}",
        f"- Analysis revision: {revision}",
        f"- Source video: {video_path}",
        f"- Logical clips: {len(clips)}",
        f"- Detected boundaries: {boundary_count}",
        f"- Analysis wall time: {elapsed_sec:.3f} s",
        "- Workflow execution: disabled (recommendations only)",
        "",
        "| Clip | Source PTS range (s) | Motion | Confidence | Recommendation | Review |",
        "|---|---:|---|---:|---|---|",
    ]
    for clip in clips:
        lines.append(
            "| {clip_id} | {start:.6f}-{end:.6f} | {motion} | {confidence:.3f} | "
            "{workflow} | {review} |".format(
                clip_id=clip["clip_id"],
                start=clip["source_start_pts_sec"],
                end=clip["source_end_pts_sec"],
                motion=clip["detected_motion_mode"],
                confidence=clip["confidence"],
                workflow=clip["recommended_workflow"],
                review="yes" if clip["needs_review"] else "no",
            )
        )
    return "\n".join(lines) + "\n"


def analyze_video(
    *,
    video_path: Path,
    output_root: Path,
    project_id: str,
    srt_path: Path | None = None,
    analysis_revision: str | None = None,
    sample_interval_sec: float = 0.5,
    ffmpeg_executable: str | Path | None = None,
    ffprobe_executable: str | Path | None = None,
    progress_callback: Callable[[str, str, float | None], None] | None = None,
) -> Path:
    def report(stage: str, message: str, fraction: float | None) -> None:
        if progress_callback is not None:
            progress_callback(stage, message, fraction)

    started = time.perf_counter()
    source = Path(video_path)
    revision = analysis_revision or _new_revision()
    report("probing_pts", "正在读取视频时间戳", 0.02)
    packet_index = probe_video_pts(source, ffmpeg_executable=ffmpeg_executable)
    report("probing_pts", "视频封装时间戳读取完成", 0.08)
    frame_index, sparse_frames = decode_indexed_sparse_frames(
        source,
        interval_sec=sample_interval_sec,
        packet_pts=frozenset(packet.pts for packet in packet_index.packets),
        ffmpeg_executable=ffmpeg_executable,
    )
    report("probing_pts", "权威展示帧索引建立完成", 0.25)
    sampled_pts: list[float] = []
    sampled_lumas: list[float] = []
    pair_evidence = []
    coarse_shot_boundaries: list[BoundaryEvidence] = []
    previous_frame = None
    duration_sec = (
        frame_index.source_end_pts_exclusive_sec - frame_index.source_start_pts_sec
    )
    expected_samples = max(2, math.ceil(duration_sec / sample_interval_sec) + 1)
    progress_stride = max(1, expected_samples // 100)
    for frame in sparse_frames:
        sampled_pts.append(frame.pts_sec)
        if len(sampled_pts) == 1 or len(sampled_pts) % progress_stride == 0:
            report(
                "sampling_frames",
                f"正在分析抽样画面 {len(sampled_pts)}/{expected_samples}",
                min(0.80, 0.25 + 0.55 * len(sampled_pts) / expected_samples),
            )
        sampled_lumas.append(float(frame.image.mean()))
        if previous_frame is not None:
            evidence = analyze_frame_pair(previous_frame, frame)
            pair_evidence.append(evidence)
            coarse_shot_boundaries.extend(
                detect_shot_boundaries(
                    [previous_frame, frame],
                    expected_interval_sec=sample_interval_sec,
                    pair_evidence=[evidence],
                )
            )
        previous_frame = frame
    if len(sampled_pts) < 2:
        raise ValueError("video analysis requires at least two decoded PTS samples")
    raw_windows = build_motion_windows(pair_evidence, MotionAnalysisConfig())
    stable_windows, motion_boundaries = stabilize_motion_windows(
        raw_windows, MotionAnalysisConfig()
    )
    verification_interval_sec = min(0.1, sample_interval_sec / 2.0)
    decode_pass_count = 1
    shot_boundaries = coarse_shot_boundaries
    if duration_sec >= 60.0 and coarse_shot_boundaries:
        report("verifying_scenes", "正在复核候选场景边界", 0.82)
        candidate_ranges = _candidate_verification_ranges(
            coarse_shot_boundaries,
            pair_evidence,
            margin_sec=verification_interval_sec,
        )
        dense_groups = decode_sparse_frame_ranges(
            source,
            index=frame_index,
            ranges=candidate_ranges,
            interval_sec=verification_interval_sec,
            ffmpeg_executable=ffmpeg_executable,
        )
        shot_boundaries = verify_candidate_boundaries(
            dense_groups,
            expected_interval_sec=verification_interval_sec,
        )
        decode_pass_count = 2

    report("segmenting", "正在检测场景边界与规划片段", 0.84)
    # Scene discontinuities are mandatory. Motion changes remain explainable
    # analysis evidence, but do not create extra fragments by themselves.
    assert previous_frame is not None
    mandatory_boundaries = _scene_boundaries_for_segmentation(
        shot_boundaries,
        source_start_pts_sec=frame_index.source_start_pts_sec,
        source_end_pts_sec=frame_index.source_end_pts_exclusive_sec,
        recent_frame_lumas=sampled_lumas,
        opening_guard_sec=max(3.0, sample_interval_sec * 6.0),
        terminal_guard_sec=max(1.0, sample_interval_sec * 2.0),
    )
    mandatory_boundaries = _mandatory_boundaries_for_source(
        mandatory_boundaries,
        source_start_pts_sec=frame_index.source_start_pts_sec,
        source_end_pts_exclusive_sec=frame_index.source_end_pts_exclusive_sec,
    )
    mandatory_boundaries = coalesce_boundaries(
        mandatory_boundaries, within_sec=sample_interval_sec * 2.0
    )
    cut_candidates = [
        CutCandidate(
            item.to_pts_sec,
            motion_magnitude_px=item.flow_magnitude_px,
            clarity_score=item.clarity_score,
        )
        for item in pair_evidence
    ]
    planned = plan_clip_intervals(
        frame_index=frame_index,
        mandatory_boundaries=mandatory_boundaries,
        cut_candidates=cut_candidates,
        config=SegmentationConfig(),
    )
    shot_boundaries = coalesce_boundaries(
        shot_boundaries, within_sec=sample_interval_sec * 2.0
    )

    srt_records: list[dict[str, Any]] = []
    if srt_path is not None:
        srt_source = Path(srt_path)
        with srt_source.open("rb") as stream:
            srt_analysis = analyze_srt_stream(
                stream,
                srt_source.name,
                video_duration_sec=frame_index.source_end_pts_exclusive_sec
                - frame_index.source_start_pts_sec,
            )
        srt_records = list(srt_analysis.get("records", []))

    clip_payloads: list[dict[str, Any]] = []
    source_rotation_verified = len(planned) == 1 and _rotation_evidence_verified(
        stable_windows,
        source_start_pts_sec=frame_index.source_start_pts_sec,
        source_end_pts_exclusive_sec=frame_index.source_end_pts_exclusive_sec,
    )
    for index, interval in enumerate(planned, start=1):
        mode, confidence = _dominant_motion(
            stable_windows, interval.start_pts_sec, interval.end_pts_sec
        )
        srt_coverage = assess_clip_srt_coverage(
            srt_records,
            clip_source_start_pts_sec=interval.start_pts_sec,
            clip_source_end_pts_sec=interval.end_pts_sec,
            video_source_start_pts_sec=frame_index.source_start_pts_sec,
        )
        recommendation = recommend_workflow(
            mode,
            confidence,
            srt_coverage,
            pure_rotation_verified=(
                source_rotation_verified and mode is MotionMode.ROTATION_DOMINANT
            ),
        )
        analysis_start, analysis_end = _analysis_range(
            sampled_pts, interval.start_pts_sec, interval.end_pts_sec
        )
        clip = LogicalClip(
            project_id=project_id,
            clip_id=f"clip-{index:04d}",
            source_start_pts_sec=interval.start_pts_sec,
            source_end_pts_sec=interval.end_pts_sec,
            analysis_start_pts_sec=analysis_start,
            analysis_end_pts_sec=analysis_end,
            start_boundary=interval.start_boundary,
            end_boundary=interval.end_boundary,
            detected_motion_mode=mode,
            confidence=confidence,
            recommended_workflow=recommendation.recommended_workflow,
            needs_review=interval.needs_review or recommendation.needs_review,
            pts_mapping=PtsMapping(interval.start_pts_sec),
            render_order=index - 1,
            analysis_revision=revision,
            source_start_pts=interval.source_start_pts,
            source_end_pts_exclusive=interval.source_end_pts_exclusive,
            source_time_base=frame_index.time_base,
            scene_index=interval.scene_index,
            segment_index=interval.segment_index,
        ).to_dict()
        clip["srt_coverage"] = srt_coverage.to_dict()
        clip["workflow_recommendation"] = {
            "recommended_workflow": recommendation.recommended_workflow,
            "auto_selected": recommendation.auto_selected,
            "reasons": list(recommendation.reasons),
        }
        clip_payloads.append(clip)

    report("publishing", "正在验证并发布分析结果", 0.94)

    detected = sorted(
        [*shot_boundaries, *motion_boundaries],
        key=lambda item: (item.pts_sec, item.reasons),
    )
    elapsed = time.perf_counter() - started
    metadata = {
        "source_path": str(source.resolve()),
        "timestamp_authority": "decoded_frame_presentation_order_pts",
        "time_base": {
            "numerator": frame_index.time_base.numerator,
            "denominator": frame_index.time_base.denominator,
        },
        "source_start_pts": frame_index.source_start_pts,
        "source_end_pts_exclusive": frame_index.source_end_pts_exclusive,
        "source_start_pts_sec": frame_index.source_start_pts_sec,
        "source_end_pts_exclusive_sec": frame_index.source_end_pts_exclusive_sec,
        # Legacy seconds alias remains for the Stage 8A artifact validator.
        "source_end_pts_sec": frame_index.source_end_pts_exclusive_sec,
        "duration_sec": frame_index.source_end_pts_exclusive_sec
        - frame_index.source_start_pts_sec,
        "decoded_frame_count": len(frame_index.frames),
        "packet_count": len(packet_index.packets),
        "sample_interval_sec": sample_interval_sec,
        "sampled_frame_count": len(sampled_pts),
        "decode_pass_count": decode_pass_count,
        "coarse_scene_candidate_count": len(coarse_shot_boundaries),
        "confirmed_scene_boundary_count": len(shot_boundaries),
    }
    configuration = {
        "sample_interval_sec": sample_interval_sec,
        "segmentation_strategy": "minimum_count_balanced_strict_lt_hard_max",
        "motion_boundaries_create_clips": False,
        "terminal_fade_guard_sec": max(1.0, sample_interval_sec * 2.0),
        "opening_transition_guard_sec": max(3.0, sample_interval_sec * 6.0),
        "shot_candidate_verification_interval_sec": verification_interval_sec,
        "motion_window_sec": MotionAnalysisConfig().window_sec,
        "motion_step_sec": MotionAnalysisConfig().step_sec,
        "motion_min_sustain_sec": MotionAnalysisConfig().min_sustain_sec,
        "motion_static_merge_max_sec": MotionAnalysisConfig().static_merge_max_sec,
        "target_clip_sec": SegmentationConfig().target_sec,
        "hard_max_clip_sec": SegmentationConfig().hard_max_sec,
        "min_clip_sec": SegmentationConfig().min_clip_sec,
    }
    payloads = {
        "video_analysis_manifest.json": _json(
            {
                "schema_version": 1,
                "status": "complete",
                "project_id": project_id,
                "analysis_revision": revision,
                "source_video": str(source.resolve()),
                "configuration": configuration,
                "clip_count": len(clip_payloads),
                "executes_workflow": False,
                "physical_video_slices_created": False,
                "elapsed_sec": elapsed,
            }
        ),
        "video_metadata.json": _json(metadata),
        "analysis_windows.csv": _windows_csv(stable_windows),
        "detected_boundaries.json": _json(
            {"analysis_revision": revision, "boundaries": [item.to_dict() for item in detected]}
        ),
        "clip_manifest.json": _json(
            {
                "schema_version": 1,
                "project_id": project_id,
                "analysis_revision": revision,
                "clips": clip_payloads,
            }
        ),
        "video_analysis_report.md": _report(
            project_id=project_id,
            revision=revision,
            video_path=source,
            elapsed_sec=elapsed,
            clips=clip_payloads,
            boundary_count=len(detected),
        ),
    }
    published = publish_analysis_revision(
        Path(output_root) / VIDEO_ANALYSIS_DIRECTORY, revision, payloads
    )
    report("complete", "视频分析完成", 1.0)
    return published
