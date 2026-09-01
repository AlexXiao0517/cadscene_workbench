from __future__ import annotations

import json
from pathlib import Path
import subprocess

from cadscene.video_analysis.analyzer import (
    _candidate_verification_ranges,
    _mandatory_boundaries_for_source,
    _rotation_evidence_verified,
    _scene_boundaries_for_segmentation,
    analyze_video,
)
from cadscene.video_analysis.motion import MotionWindow
from cadscene.video_analysis.models import MotionMode
from cadscene.video_analysis.artifacts import REQUIRED_ARTIFACTS
from cadscene.video_analysis.models import BoundaryEvidence
from cadscene.video_analysis.pts import resolve_ffmpeg_executable
from cadscene.video_analysis.shot_detection import FramePairEvidence


def _make_short_video(path: Path) -> Path:
    ffmpeg = resolve_ffmpeg_executable()
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=4",
            "-c:v",
            "ffv1",
            str(path),
        ],
        check=True,
    )
    return path


def test_short_video_end_to_end_publishes_one_explainable_pts_clip(tmp_path: Path) -> None:
    video = _make_short_video(tmp_path / "short.mkv")
    progress: list[tuple[str, str, float | None]] = []

    published = analyze_video(
        video_path=video,
        output_root=tmp_path / "run",
        project_id="project-short",
        analysis_revision="analysis-test-0001",
        sample_interval_sec=0.5,
        progress_callback=lambda stage, message, fraction: progress.append(
            (stage, message, fraction)
        ),
    )

    output = tmp_path / "run" / "v"
    assert published.parent == output / "r"
    assert published.name.startswith("r-")
    assert len(published.name) == 18
    assert all((output / name).is_file() for name in REQUIRED_ARTIFACTS)

    metadata = json.loads((output / "video_metadata.json").read_text(encoding="utf-8"))
    clips = json.loads((output / "clip_manifest.json").read_text(encoding="utf-8"))[
        "clips"
    ]
    manifest = json.loads(
        (output / "video_analysis_manifest.json").read_text(encoding="utf-8")
    )

    assert metadata["timestamp_authority"] == "decoded_frame_presentation_order_pts"
    assert metadata["source_start_pts"] == 0
    assert metadata["source_end_pts_exclusive"] > metadata["source_start_pts"]
    assert metadata["source_start_pts_sec"] == 0.0
    assert metadata["source_end_pts_exclusive_sec"] == 4.0
    assert metadata["sample_interval_sec"] == 0.5
    assert metadata["sampled_frame_count"] > 1
    assert metadata["decode_pass_count"] == 1
    assert len(clips) == 1
    clip = clips[0]
    assert clip["project_id"] == "project-short"
    assert clip["clip_id"] == "clip-0001"
    assert clip["source_start_pts"] == metadata["source_start_pts"]
    assert (
        clip["source_end_pts_exclusive"]
        == metadata["source_end_pts_exclusive"]
    )
    assert clip["source_time_base"] == metadata["time_base"]
    assert clip["interval_semantics"] == "half_open"
    assert clip["source_start_pts_sec"] == metadata["source_start_pts_sec"]
    assert (
        clip["source_end_pts_exclusive_sec"]
        == metadata["source_end_pts_exclusive_sec"]
    )
    assert (
        clip["source_end_pts_exclusive_sec"] - clip["source_start_pts_sec"] < 60.0
    )
    assert clip["analysis_start_pts_sec"] >= clip["source_start_pts_sec"]
    assert clip["analysis_end_pts_sec"] <= clip["source_end_pts_sec"]
    assert clip["start_boundary"]["reasons"] == ["source_start"]
    assert clip["end_boundary"]["reasons"] == ["source_end"]
    assert clip["detected_motion_mode"] in {
        "general_motion",
        "rotation_dominant",
        "static",
        "unknown",
    }
    assert clip["recommended_workflow"] in {
        "sfm_only",
        "pure_rotation",
        "srt_sfm_fused",
        "srt_full_pose",
    }
    assert clip["workflow_recommendation"]["auto_selected"] is False
    assert clip["pts_mapping"]["clip_to_source_pts_offset_sec"] == 0.0
    assert clip["render_order"] == 0
    assert clip["scene_index"] == 1
    assert clip["segment_index"] == 1
    assert clip["analysis_revision"] == "analysis-test-0001"
    assert manifest["executes_workflow"] is False
    assert not list(output.rglob("*.mp4"))
    measured = [item[2] for item in progress if item[2] is not None]
    assert progress[0][0] == "probing_pts"
    assert progress[-1] == ("complete", "视频分析完成", 1.0)
    assert measured == sorted(measured)
    assert any(stage == "sampling_frames" for stage, _message, _value in progress)


def test_short_source_keeps_one_logical_clip_despite_detected_boundaries() -> None:
    boundaries = [
        BoundaryEvidence(19.019, ("image_discontinuity",), 0.98),
        BoundaryEvidence(21.5215, ("image_discontinuity",), 0.98),
    ]

    assert _mandatory_boundaries_for_source(
        boundaries,
        source_start_pts_sec=0.0,
        source_end_pts_exclusive_sec=37.78775,
    ) == []


def test_short_sustained_rotation_is_verified_for_recommendation() -> None:
    windows = [
        MotionWindow(
            start_pts_sec=float(index),
            end_pts_sec=float(index + 4),
            motion_mode=(
                MotionMode.STATIC if index in {8, 9} else MotionMode.ROTATION_DOMINANT
            ),
            confidence=0.92,
            evidence_count=9,
            median_flow_px=0.3 if index in {8, 9} else 8.0,
            median_residual_px=0.8,
            median_homography_inlier_ratio=0.94,
        )
        for index in range(20)
    ]

    assert _rotation_evidence_verified(
        windows,
        source_start_pts_sec=0.0,
        source_end_pts_exclusive_sec=23.0,
    ) is True


def test_rotation_recommendation_verification_rejects_general_motion_counterevidence() -> None:
    windows = [
        MotionWindow(
            start_pts_sec=float(index),
            end_pts_sec=float(index + 4),
            motion_mode=(
                MotionMode.GENERAL_MOTION
                if index >= 8
                else MotionMode.ROTATION_DOMINANT
            ),
            confidence=0.92,
            evidence_count=9,
            median_flow_px=8.0,
            median_residual_px=0.8,
            median_homography_inlier_ratio=0.94,
        )
        for index in range(12)
    ]

    assert _rotation_evidence_verified(
        windows,
        source_start_pts_sec=0.0,
        source_end_pts_exclusive_sec=15.0,
    ) is False


def test_end_to_end_full_pose_srt_coverage_precedes_visual_motion(tmp_path: Path) -> None:
    video = _make_short_video(tmp_path / "short-with-srt.mkv")
    progress: list[tuple[str, str, float | None]] = []
    blocks = []
    for second in range(4):
        blocks.append(
            "\n".join(
                [
                    str(second + 1),
                    f"00:00:0{second},000 --> 00:00:0{second + 1},000",
                    "[latitude: 30.0] [longitude: 120.0] [altitude: 50.0] "
                    "[gimbal_yaw: 1.0] [gimbal_pitch: -20.0] [gimbal_roll: 0.0]",
                ]
            )
        )
    srt = tmp_path / "full.srt"
    srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")

    analyze_video(
        video_path=video,
        output_root=tmp_path / "srt-run",
        project_id="project-srt",
        srt_path=srt,
        analysis_revision="analysis-test-srt",
        sample_interval_sec=0.5,
        progress_callback=lambda stage, message, fraction: progress.append(
            (stage, message, fraction)
        ),
    )

    clip = json.loads(
        (tmp_path / "srt-run" / "v" / "clip_manifest.json").read_text(
            encoding="utf-8"
        )
    )["clips"][0]
    assert clip["srt_coverage"]["kind"] == "full_pose"
    assert clip["recommended_workflow"] == "srt_full_pose"
    assert clip["workflow_recommendation"]["auto_selected"] is False
    stages = [stage for stage, _message, _fraction in progress]
    assert stages.index("parsing_srt") < stages.index("routing_clips")
    assert stages.index("routing_clips") < stages.index("publishing")


def test_terminal_fade_is_reported_but_does_not_create_tiny_tail_clip() -> None:
    boundaries = [
        BoundaryEvidence(40.0, ("image_discontinuity",), .9),
        BoundaryEvidence(99.5, ("image_discontinuity",), .9),
    ]

    selected = _scene_boundaries_for_segmentation(
        boundaries,
        source_end_pts_sec=100.0,
        # Includes the measured terminal sparse-frame luma (15.82) from
        # jinhuaorigin.mp4 and confirms a multi-sample monotonic fade.
        recent_frame_lumas=[82.0, 44.0, 15.82],
        terminal_guard_sec=1.0,
    )

    assert [item.pts_sec for item in selected] == [40.0]


def test_terminal_boundary_is_kept_when_video_does_not_end_on_black() -> None:
    boundary = BoundaryEvidence(99.5, ("image_discontinuity",), .9)

    selected = _scene_boundaries_for_segmentation(
        [boundary],
        source_end_pts_sec=100.0,
        recent_frame_lumas=[90.0, 90.0, 90.0],
        terminal_guard_sec=1.0,
    )

    assert selected == [boundary]


def test_single_hard_cut_to_dark_near_end_remains_mandatory() -> None:
    boundary = BoundaryEvidence(99.5, ("image_discontinuity",), .9)

    selected = _scene_boundaries_for_segmentation(
        [boundary],
        source_end_pts_sec=100.0,
        recent_frame_lumas=[90.0, 90.0, 10.0],
        terminal_guard_sec=1.0,
    )

    assert selected == [boundary]


def test_short_black_transition_uses_first_stable_nonblack_boundary() -> None:
    selected = _scene_boundaries_for_segmentation(
        [
            BoundaryEvidence(5.04, ("black_frame",), 0.97),
            BoundaryEvidence(
                5.12, ("black_frame", "image_discontinuity"), 0.98
            ),
            BoundaryEvidence(20.0, ("image_discontinuity",), 0.91),
        ],
        source_end_pts_sec=30.0,
        recent_frame_lumas=[90.0, 90.0, 90.0],
        terminal_guard_sec=1.0,
    )

    assert [boundary.pts_sec for boundary in selected] == [5.12, 20.0]


def test_opening_black_transition_is_kept_as_diagnostic_but_not_a_scene_cut() -> None:
    opening = BoundaryEvidence(
        2.002, ("black_frame", "exposure_discontinuity"), 0.98
    )
    actual_cut = BoundaryEvidence(20.0, ("image_discontinuity",), 0.94)

    selected = _scene_boundaries_for_segmentation(
        [opening, actual_cut],
        source_start_pts_sec=0.0,
        source_end_pts_sec=100.0,
        recent_frame_lumas=[90.0, 90.0, 90.0],
        opening_guard_sec=3.0,
        terminal_guard_sec=1.0,
    )

    assert selected == [actual_cut]


def test_candidate_verification_ranges_cover_the_entire_coarse_pair() -> None:
    evidence = [
        FramePairEvidence(
            from_pts_sec=53.5535,
            to_pts_sec=54.054,
            image_change_score=0.5,
            black_frame_score=0.0,
            exposure_jump_score=0.0,
            feature_match_count=2,
            feature_spatial_coverage=0.0,
            homography_inlier_ratio=0.0,
            flow_magnitude_px=10.0,
            flow_residual_px=20.0,
            clarity_score=0.8,
        )
    ]

    ranges = _candidate_verification_ranges(
        [BoundaryEvidence(54.054, ("image_discontinuity",), 0.9)],
        evidence,
        margin_sec=0.1,
    )

    assert ranges == [(53.4535, 54.154)]
