import io
from pathlib import Path

from cadscene.srt.capability import detect_trajectory_capability
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

    assert analysis["detected_mode"] == "srt_fixed_track_visual_pose"
    assert analysis["attitude_sources"]["drone"] is True
    assert analysis["attitude_sources"]["gimbal"] is False


def test_missing_gps_is_sfm_only_with_warning():
    analysis = analyze("gps_missing.srt")

    assert analysis["detected_mode"] == "sfm_only"
    assert any("GPS" in warning for warning in analysis["warnings"])


def test_non_geographic_gps_values_never_enable_srt_trajectory_modes():
    records = [
        {"latitude": 91.0, "longitude": 120.0, "altitude": 50.0},
        {"latitude": 30.0, "longitude": 181.0, "altitude": 50.0},
    ]

    analysis = detect_trajectory_capability(records)

    assert analysis["coverage"]["latitude"] == 0.5
    assert analysis["coverage"]["longitude"] == 0.5
    assert analysis["detected_mode"] == "sfm_only"


def test_srt_analysis_reads_stream_in_bounded_chunks():
    class BoundedReadStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert size > 0, "analyze_srt_stream must not issue an unbounded read"
            return super().read(min(size, 17))

    stream = BoundedReadStream((FIXTURES / "full_pose_example.srt").read_bytes())

    analysis = analyze_srt_stream(stream, "full_pose_example.srt")

    assert analysis["detected_mode"] == "srt_full_pose"
    assert len(analysis["records"]) == 2


def test_partial_metadata_is_fused_but_not_full_pose():
    analysis = analyze("no_attitude_partial.srt")

    assert analysis["detected_mode"] == "srt_fixed_track_visual_pose"
    assert analysis["fields"]["yaw"] is False


def test_malformed_srt_falls_back_to_sfm_only_with_warning():
    analysis = analyze("malformed.srt")

    assert analysis["detected_mode"] == "sfm_only"
    assert any("parse" in warning.lower() for warning in analysis["warnings"])


def test_duration_mismatch_adds_warning_without_changing_full_pose():
    analysis = analyze("full_pose_example.srt", duration=30.0)

    assert analysis["detected_mode"] == "srt_full_pose"
    assert any("duration" in warning.lower() for warning in analysis["warnings"])


def test_marginal_attitude_coverage_without_cooccurring_pose_is_not_full_pose():
    records = [
        {"latitude": 30.0, "longitude": 120.0, "altitude": 50.0, "rel_alt": 50.0, "gimbal_yaw": 1.0, "gimbal_pitch": 2.0, "gimbal_roll": 3.0},
        {"latitude": 30.1, "longitude": 120.1, "altitude": 50.1, "rel_alt": 50.1, "gimbal_yaw": 1.0, "gimbal_pitch": 2.0, "gimbal_roll": 3.0},
        {"latitude": 30.2, "longitude": 120.2, "altitude": 50.2, "rel_alt": 50.2, "gimbal_yaw": 1.0, "gimbal_pitch": 2.0},
        {"latitude": 30.3, "longitude": 120.3, "altitude": 50.3, "rel_alt": 50.3, "gimbal_yaw": 1.0, "gimbal_roll": 3.0},
        {"latitude": 30.4, "longitude": 120.4, "altitude": 50.4, "rel_alt": 50.4, "gimbal_pitch": 2.0, "gimbal_roll": 3.0},
    ]

    analysis = detect_trajectory_capability(records)

    assert analysis["coverage"]["gimbal_yaw"] == 0.8
    assert analysis["detected_mode"] == "srt_fixed_track_visual_pose"


def test_absolute_height_without_relative_height_does_not_enable_fixed_track():
    records = [
        {"latitude": 30.0, "longitude": 120.0, "abs_alt": 50.0},
        {"latitude": 30.1, "longitude": 120.1, "abs_alt": 50.1},
    ]

    analysis = detect_trajectory_capability(records)

    assert analysis["detected_mode"] == "sfm_only"
    assert any("relative height" in warning.lower() for warning in analysis["warnings"])


def test_dji_rel_alt_and_gb_attitude_aliases_produce_full_pose():
    analysis = analyze("dji_short_aliases.srt")

    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["fields"]["altitude"] is True
    assert analysis["fields"]["yaw"] is True
