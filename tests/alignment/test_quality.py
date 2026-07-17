from __future__ import annotations

from pathlib import Path

from cadscene.alignment.quality import (
    QualityConfig,
    SuggestionConfig,
    augment_camera_track_with_quality,
    build_keyframe_suggestions_payload,
    build_reason_codes,
    combined_risk_score,
    evaluate_quality,
    normalize_weights,
    risk_level,
    select_suggestions,
)
from cadscene.core.io import write_csv_utf8_sig


def _path_rows() -> list[dict]:
    return [
        {"frame_index": 0, "camera_x": 0.0, "camera_y": 0.0, "camera_z": 1.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
        {"frame_index": 40, "camera_x": 3.0, "camera_y": 0.0, "camera_z": 1.0, "yaw": 5.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
        {"frame_index": 80, "camera_x": 6.0, "camera_y": 1.0, "camera_z": 1.0, "yaw": 35.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
        {"frame_index": 120, "camera_x": 9.0, "camera_y": 1.0, "camera_z": 1.0, "yaw": 40.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
    ]


def _track() -> dict:
    return {
        "keyframes": [
            {"frame": 0, "source": "manual_keyframe", "camera": {"x": 0.0, "y": 0.0, "z": 1.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0}},
            {"frame": 80, "source": "algorithm_prediction", "camera": {"x": 6.0, "y": 1.0, "z": 1.0, "yaw": 35.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0}},
            {"frame": 120, "source": "confirmed_keyframe", "camera": {"x": 9.0, "y": 1.0, "z": 1.0, "yaw": 40.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0}},
        ]
    }


def _alignment() -> dict:
    return {
        "schema_version": "cadscene_alignment_v1",
        "residuals": {
            "frames": [0, 40, 80, 120],
            "position_m": [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [6.0, 0.0, 0.0], [9.0, 0.0, 0.0]],
            "angle_deg": [[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0], [4.0, 0.0, 0.0]],
        },
    }


def test_weights_missing_signals_are_renormalized() -> None:
    weights = normalize_weights({"anchor_distance": 0.3, "sfm_quality": 0.7}, {"anchor_distance": True, "sfm_quality": False})

    assert weights == {"anchor_distance": 1.0}
    assert combined_risk_score({"anchor_distance": 0.4, "sfm_quality": None}, weights) == 0.4


def test_risk_level_and_reason_code_thresholds() -> None:
    assert risk_level(0.34) == "low"
    assert risk_level(0.35) == "medium"
    assert risk_level(0.65) == "high"
    codes = build_reason_codes({"anchor_distance": 0.59, "turn_motion": 0.6}, registered=True, reason_threshold=0.6)

    assert codes == ["high_turn_motion"]


def test_bootstrap_ignores_algorithm_prediction_and_disables_correction() -> None:
    result = evaluate_quality(
        sfm_camera_path_rows=_path_rows(),
        alignment_json=_alignment(),
        web_camera_track=_track(),
        trajectory_json=None,
        config=QualityConfig(quality_mode="bootstrap"),
    )

    assert result.meta["confirmed_anchor_frames"] == [0, 120]
    assert result.signal_availability["correction"] is False
    assert all(row["correction_risk"] == "unavailable" for row in result.timeline)


def test_qa_allows_correction_and_risk_scores_stay_in_range() -> None:
    result = evaluate_quality(
        sfm_camera_path_rows=_path_rows(),
        alignment_json=_alignment(),
        web_camera_track=_track(),
        trajectory_json=None,
        config=QualityConfig(quality_mode="qa"),
    )

    assert result.signal_availability["correction"] is True
    assert all(0.0 <= float(row["risk_score"]) <= 1.0 for row in result.timeline)
    assert any(row["correction_risk"] != "unavailable" for row in result.timeline)


def test_suggestion_nms_gap_and_max_count() -> None:
    timeline = [
        {"frame_index": 10, "risk_score": 0.9, "risk_level": "high", "reason_codes": "a", "nearest_anchor_frame": 0, "suggest_action": "检查"},
        {"frame_index": 20, "risk_score": 0.95, "risk_level": "high", "reason_codes": "b", "nearest_anchor_frame": 0, "suggest_action": "检查"},
        {"frame_index": 80, "risk_score": 0.8, "risk_level": "high", "reason_codes": "c", "nearest_anchor_frame": 120, "suggest_action": "检查"},
        {"frame_index": 140, "risk_score": 0.7, "risk_level": "high", "reason_codes": "d", "nearest_anchor_frame": 120, "suggest_action": "检查"},
    ]

    suggestions = select_suggestions(timeline, SuggestionConfig(max_suggestions=2, min_suggestion_gap=40, suggestion_risk_threshold=0.65))

    assert [row["frame_index"] for row in suggestions] == [20, 80]


def test_adaptive_percentile_prevents_full_saturation() -> None:
    result = evaluate_quality(
        sfm_camera_path_rows=_path_rows(),
        alignment_json=_alignment(),
        web_camera_track=_track(),
        trajectory_json=None,
        config=QualityConfig(quality_mode="qa", adaptive_percentile=85.0),
    )
    correction_values = [float(row["correction_risk"]) for row in result.timeline]

    assert any(0.0 < value < 1.0 for value in correction_values)


def test_augmented_camera_track_keeps_manual_camera_and_adds_quality_meta() -> None:
    result = evaluate_quality(
        sfm_camera_path_rows=_path_rows(),
        alignment_json=_alignment(),
        web_camera_track=_track(),
        trajectory_json=None,
        config=QualityConfig(quality_mode="qa"),
    )
    original_camera = dict(_track()["keyframes"][0]["camera"])

    augmented = augment_camera_track_with_quality(_track(), result.timeline, result.suggestion_frames, result.meta)

    assert augmented["keyframes"][0]["camera"] == original_camera
    assert "quality" in augmented["keyframes"][0]
    assert augmented["meta"]["generated_by"] == "cadscene.evaluate_quality"


def test_keyframe_suggestions_payload_meta_and_csv_bom(tmp_path: Path) -> None:
    result = evaluate_quality(
        sfm_camera_path_rows=_path_rows(),
        alignment_json=_alignment(),
        web_camera_track=_track(),
        trajectory_json=None,
        config=QualityConfig(quality_mode="qa", max_suggestions=3, min_suggestion_gap=10, suggestion_risk_threshold=0.1),
    )
    payload = build_keyframe_suggestions_payload(result.suggestions, result.meta)
    csv_path = tmp_path / "quality_timeline.csv"
    write_csv_utf8_sig(csv_path, result.timeline)

    assert payload["meta"]["quality_mode"] == "qa"
    assert payload["meta"]["confirmed_anchor_count"] == 2
    assert payload["meta"]["leakage_guard_enabled"] is True
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
