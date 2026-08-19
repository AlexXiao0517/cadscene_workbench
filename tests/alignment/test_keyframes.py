import json

from cadscene.alignment.keyframes import confirmed_keyframes, load_web_camera_track
from cadscene.core.coordinates import web_camera_to_python_state


def test_confirmed_keyframes_skip_algorithm_prediction_and_sort():
    track = {
        "keyframes": [
            {"frame": 0, "camera": {"x": 0}},
            {"frame": 30, "source": "algorithm_prediction", "camera": {"x": 1}},
            {"frame": 20, "source": "confirmed_keyframe", "camera": {"x": 2}},
            {"frame": 10, "source": "manual_keyframe", "camera": {"x": 3}},
            {"frame": 40, "source": "manual_keyframe"},
        ]
    }

    frames = [item["frame"] for item in confirmed_keyframes(track)]

    assert frames == [10, 20]


def test_confirmed_keyframes_accept_current_explicit_manual_sources():
    sources = ["manual", "confirmed", "manual_anchor", "manual_corrected"]
    track = {
        "keyframes": [
            {"frame": frame, "source": source, "camera": {"x": frame}}
            for frame, source in enumerate(sources)
        ]
    }

    assert [item["source"] for item in confirmed_keyframes(track)] == sources


def test_confirmed_keyframes_accept_legacy_track_only_when_multiple_poses_exist():
    legacy = {
        "keyframes": [
            {"frame": 0, "camera": {"x": 0}},
            {"frame": 10, "camera": {"x": 1}},
        ]
    }

    assert [item["frame"] for item in confirmed_keyframes(legacy)] == [0, 10]
    assert confirmed_keyframes({"keyframes": [legacy["keyframes"][0]]}) == []


def test_load_web_camera_track_reads_utf8_sig(tmp_path):
    path = tmp_path / "camera_track.json"
    path.write_text(json.dumps({"keyframes": []}, ensure_ascii=False), encoding="utf-8-sig")

    assert load_web_camera_track(path) == {"keyframes": []}


def test_keyframe_conversion_matches_legacy_pitch_rule():
    state = web_camera_to_python_state(
        {"x": 101.0, "y": 202.0, "z": 50.0, "pitch": -30.0},
        origin_xy=(100.0, 200.0),
        cad_scale=0.25,
    )

    assert state.camera_x == 0.25
    assert state.camera_y == 0.5
    assert state.camera_z == 12.5
    assert state.pitch_deg == 30.0
