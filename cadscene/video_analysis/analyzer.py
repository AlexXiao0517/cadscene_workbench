from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import time
from typing import Any

from cadscene.srt.parser import analyze_srt_stream

from .artifacts import publish_analysis_revision
from .models import BoundaryEvidence, LogicalClip, MotionMode, PtsMapping
from .motion import (
    MotionAnalysisConfig,
    MotionWindow,
    build_motion_windows,
    stabilize_motion_windows,
)
from .pts import decode_sparse_frames, probe_video_pts
from .recommendation import assess_clip_srt_coverage, recommend_workflow
from .segmentation import CutCandidate, SegmentationConfig, plan_clip_intervals
from .shot_detection import analyze_frame_pair, detect_shot_boundaries


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


def _analysis_range(frames, start_pts_sec: float, end_pts_sec: float) -> tuple[float, float]:
    pts = [frame.pts_sec for frame in frames if start_pts_sec <= frame.pts_sec <= end_pts_sec]
    if not pts:
        return start_pts_sec, end_pts_sec
    return min(pts), max(pts)


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
) -> Path:
    started = time.perf_counter()
    source = Path(video_path)
    revision = analysis_revision or _new_revision()
    pts_index = probe_video_pts(source, ffmpeg_executable=ffmpeg_executable)
    frames = decode_sparse_frames(
        source,
        index=pts_index,
        interval_sec=sample_interval_sec,
        ffmpeg_executable=ffmpeg_executable,
    )
    if len(frames) < 2:
        raise ValueError("video analysis requires at least two decoded PTS samples")

    pair_evidence = [analyze_frame_pair(before, after) for before, after in zip(frames, frames[1:])]
    shot_boundaries = detect_shot_boundaries(
        frames,
        expected_interval_sec=sample_interval_sec,
        pair_evidence=pair_evidence,
    )
    raw_windows = build_motion_windows(pair_evidence, MotionAnalysisConfig())
    stable_windows, motion_boundaries = stabilize_motion_windows(
        raw_windows, MotionAnalysisConfig()
    )
    mandatory_boundaries = [*shot_boundaries, *motion_boundaries]
    cut_candidates = [
        CutCandidate(
            item.to_pts_sec,
            motion_magnitude_px=item.flow_magnitude_px,
            clarity_score=item.clarity_score,
        )
        for item in pair_evidence
    ]
    planned = plan_clip_intervals(
        source_start_pts_sec=pts_index.source_start_pts_sec,
        source_end_pts_sec=pts_index.source_end_pts_sec,
        mandatory_boundaries=mandatory_boundaries,
        available_source_pts=[packet.pts_sec for packet in pts_index.packets],
        cut_candidates=cut_candidates,
        config=SegmentationConfig(),
    )

    srt_records: list[dict[str, Any]] = []
    if srt_path is not None:
        srt_source = Path(srt_path)
        with srt_source.open("rb") as stream:
            srt_analysis = analyze_srt_stream(
                stream,
                srt_source.name,
                video_duration_sec=pts_index.source_end_pts_sec
                - pts_index.source_start_pts_sec,
            )
        srt_records = list(srt_analysis.get("records", []))

    clip_payloads: list[dict[str, Any]] = []
    for index, interval in enumerate(planned, start=1):
        mode, confidence = _dominant_motion(
            stable_windows, interval.start_pts_sec, interval.end_pts_sec
        )
        srt_coverage = assess_clip_srt_coverage(
            srt_records,
            clip_source_start_pts_sec=interval.start_pts_sec,
            clip_source_end_pts_sec=interval.end_pts_sec,
            video_source_start_pts_sec=pts_index.source_start_pts_sec,
        )
        recommendation = recommend_workflow(mode, confidence, srt_coverage)
        analysis_start, analysis_end = _analysis_range(
            frames, interval.start_pts_sec, interval.end_pts_sec
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
        ).to_dict()
        clip["srt_coverage"] = srt_coverage.to_dict()
        clip["workflow_recommendation"] = {
            "recommended_workflow": recommendation.recommended_workflow,
            "auto_selected": recommendation.auto_selected,
            "reasons": list(recommendation.reasons),
        }
        clip_payloads.append(clip)

    detected = sorted(
        mandatory_boundaries,
        key=lambda item: (item.pts_sec, item.reasons),
    )
    elapsed = time.perf_counter() - started
    metadata = {
        "source_path": str(source.resolve()),
        "timestamp_authority": "source_packet_and_decoded_frame_pts",
        "time_base": {
            "numerator": pts_index.time_base.numerator,
            "denominator": pts_index.time_base.denominator,
        },
        "source_start_pts_sec": pts_index.source_start_pts_sec,
        "source_end_pts_sec": pts_index.source_end_pts_sec,
        "duration_sec": pts_index.source_end_pts_sec - pts_index.source_start_pts_sec,
        "packet_count": len(pts_index.packets),
        "sample_interval_sec": sample_interval_sec,
        "sampled_frame_count": len(frames),
    }
    configuration = {
        "sample_interval_sec": sample_interval_sec,
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
    return publish_analysis_revision(
        Path(output_root) / "02_video_analysis", revision, payloads
    )
