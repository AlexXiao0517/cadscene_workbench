from __future__ import annotations

import json
from pathlib import Path
import subprocess

from cadscene.video_analysis.analyzer import (
    _scene_boundaries_for_segmentation,
    analyze_video,
)
from cadscene.video_analysis.artifacts import REQUIRED_ARTIFACTS
from cadscene.video_analysis.models import BoundaryEvidence
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


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

    published = analyze_video(
        video_path=video,
        output_root=tmp_path / "run",
        project_id="project-short",
        analysis_revision="analysis-test-0001",
        sample_interval_sec=0.5,
    )

    output = tmp_path / "run" / "02_video_analysis"
    assert published == output / "analysis_revisions" / "analysis-test-0001"
    assert all((output / name).is_file() for name in REQUIRED_ARTIFACTS)

    metadata = json.loads((output / "video_metadata.json").read_text(encoding="utf-8"))
    clips = json.loads((output / "clip_manifest.json").read_text(encoding="utf-8"))[
        "clips"
    ]
    manifest = json.loads(
        (output / "video_analysis_manifest.json").read_text(encoding="utf-8")
    )

    assert metadata["timestamp_authority"] == "source_packet_and_decoded_frame_pts"
    assert metadata["source_start_pts_sec"] == 0.0
    assert metadata["source_end_pts_sec"] == 4.0
    assert metadata["sample_interval_sec"] == 0.5
    assert metadata["sampled_frame_count"] > 1
    assert len(clips) == 1
    clip = clips[0]
    assert clip["project_id"] == "project-short"
    assert clip["clip_id"] == "clip-0001"
    assert clip["source_start_pts_sec"] == metadata["source_start_pts_sec"]
    assert clip["source_end_pts_sec"] == metadata["source_end_pts_sec"]
    assert clip["source_end_pts_sec"] - clip["source_start_pts_sec"] <= 60.0
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
    assert clip["analysis_revision"] == "analysis-test-0001"
    assert manifest["executes_workflow"] is False
    assert not list(output.rglob("*.mp4"))


def test_end_to_end_full_pose_srt_coverage_precedes_visual_motion(tmp_path: Path) -> None:
    video = _make_short_video(tmp_path / "short-with-srt.mkv")
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
    )

    clip = json.loads(
        (tmp_path / "srt-run" / "02_video_analysis" / "clip_manifest.json").read_text(
            encoding="utf-8"
        )
    )["clips"][0]
    assert clip["srt_coverage"]["kind"] == "full_pose"
    assert clip["recommended_workflow"] == "srt_full_pose"
    assert clip["workflow_recommendation"]["auto_selected"] is False


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
