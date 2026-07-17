from pathlib import Path

from cadscene.srt.parser import analyze_srt_stream


FIXTURES = Path(__file__).parents[1] / "fixtures" / "srt"


def analyze(name: str, duration: float | None = None) -> dict:
    with (FIXTURES / name).open("rb") as stream:
        return analyze_srt_stream(stream, name, video_duration_sec=duration)


def test_full_camera_attitude_is_classified_as_full_pose():
    analysis = analyze("full_pose_example.srt")

    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["fields"]["yaw"] is True
    assert analysis["attitude_sources"]["gimbal"] is True


def test_drone_attitude_without_gimbal_is_partial():
    analysis = analyze("drone_attitude_only.srt")

    assert analysis["detected_mode"] == "srt_sfm_fused"
    assert analysis["attitude_sources"]["drone"] is True
    assert analysis["attitude_sources"]["gimbal"] is False


def test_missing_gps_is_sfm_only_with_warning():
    analysis = analyze("gps_missing.srt")

    assert analysis["detected_mode"] == "sfm_only"
    assert any("GPS" in warning for warning in analysis["warnings"])


def test_partial_metadata_is_fused_but_not_full_pose():
    analysis = analyze("no_attitude_partial.srt")

    assert analysis["detected_mode"] == "srt_sfm_fused"
    assert analysis["fields"]["yaw"] is False


def test_malformed_srt_falls_back_to_sfm_only_with_warning():
    analysis = analyze("malformed.srt")

    assert analysis["detected_mode"] == "sfm_only"
    assert any("parse" in warning.lower() for warning in analysis["warnings"])


def test_duration_mismatch_adds_warning_without_changing_full_pose():
    analysis = analyze("full_pose_example.srt", duration=30.0)

    assert analysis["detected_mode"] == "srt_full_pose"
    assert any("duration" in warning.lower() for warning in analysis["warnings"])
