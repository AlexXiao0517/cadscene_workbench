from __future__ import annotations

from cadscene.video_analysis.models import (
    BoundaryEvidence,
    LogicalClip,
    MotionMode,
    PtsMapping,
)


def test_clip_contract_preserves_source_pts_and_future_project_fields() -> None:
    clip = LogicalClip(
        project_id="project-7",
        clip_id="clip-0003",
        source_start_pts_sec=12.125,
        source_end_pts_sec=54.875,
        analysis_start_pts_sec=11.625,
        analysis_end_pts_sec=55.375,
        start_boundary=BoundaryEvidence(12.125, ("shot_cut",), 0.97),
        end_boundary=BoundaryEvidence(54.875, ("motion_mode_change",), 0.88),
        detected_motion_mode=MotionMode.GENERAL_MOTION,
        confidence=0.91,
        recommended_workflow="sfm_only",
        needs_review=False,
        pts_mapping=PtsMapping(clip_to_source_pts_offset_sec=12.125),
        render_order=3,
        analysis_revision="analysis-0004",
    )

    payload = clip.to_dict()

    assert payload["source_start_pts_sec"] == 12.125
    assert payload["source_end_pts_sec"] == 54.875
    assert payload["analysis_start_pts_sec"] == 11.625
    assert payload["analysis_end_pts_sec"] == 55.375
    assert payload["detected_motion_mode"] == "general_motion"
    assert payload["start_boundary"]["reasons"] == ["shot_cut"]
    assert payload["recommended_workflow"] == "sfm_only"
    assert payload["analysis_revision"] == "analysis-0004"


def test_clip_source_pts_mapping_is_exact_and_invertible() -> None:
    mapping = PtsMapping(clip_to_source_pts_offset_sec=101.375)

    assert mapping.clip_to_source_pts(2.625) == 104.0
    assert mapping.source_to_clip_pts(104.0) == 2.625


def test_motion_mode_contract_contains_all_required_states() -> None:
    assert {mode.value for mode in MotionMode} == {
        "general_motion",
        "rotation_dominant",
        "static",
        "unknown",
    }

