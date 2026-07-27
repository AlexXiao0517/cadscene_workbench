from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from cadscene.alignment.aligner import (
    AlignmentConfig,
    KeyframeCorrespondence,
    _camera_to_world_rotation,
    apply_segment_anchoring,
    aligned_state_at_frame,
    build_correspondences,
    estimate_global_sim3,
    generate_aligned_camera_path,
    generate_camera_track_pred,
    run_alignment,
)
from cadscene.core.camera import CameraState
from cadscene.core.coordinates import python_state_to_web_camera
from cadscene.core.io import write_csv_utf8_sig
from cadscene.core.sim3 import Sim3
from cadscene.sfm.trajectory import SfmTrajectory


def _trajectory() -> SfmTrajectory:
    return SfmTrajectory(
        frames=np.asarray([0, 10, 20, 30], dtype=np.int64),
        centers=np.asarray(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [1.0, 1.0, 0.0],
                [2.0, 1.0, 0.0],
            ],
            dtype=np.float64,
        ),
        quats_c2w_wxyz=np.asarray([[1.0, 0.0, 0.0, 0.0]] * 4, dtype=np.float64),
        fps=25.0,
        width=1920,
        height=1080,
        intrinsics={"width": 1920, "params": [960.0]},
    )


def _matrix_to_quat_wxyz(matrix: np.ndarray) -> list[float]:
    m = np.asarray(matrix, dtype=np.float64)
    trace = float(np.trace(m))
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        return [0.25 * s, (m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s]
    idx = int(np.argmax(np.diag(m)))
    if idx == 0:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        return [(m[2, 1] - m[1, 2]) / s, 0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s]
    if idx == 1:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        return [(m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s]
    s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
    return [(m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s]


def _track_from_states(states: dict[int, CameraState], *, origin_xy=(1000.0, 2000.0)) -> dict:
    return {
        "keyframes": [
            {
                "frame": frame,
                "source": "manual_keyframe",
                "camera": python_state_to_web_camera(state, origin_xy),
            }
            for frame, state in sorted(states.items())
        ]
    }


def _write_trajectory(path: Path, traj: SfmTrajectory) -> None:
    path.write_text(
        json.dumps(
            {
                "fps": traj.fps,
                "width": traj.width,
                "height": traj.height,
                "intrinsics": [traj.intrinsics],
                "poses": [
                    {
                        "frame_index": int(frame),
                        "center": center.tolist(),
                        "cam_from_world_quat_wxyz": quat.tolist(),
                    }
                    for frame, center, quat in zip(traj.frames, traj.centers, traj.quats_c2w_wxyz)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_estimate_global_sim3_recovers_known_transform() -> None:
    traj = _trajectory()
    orientation = CameraState(yaw_deg=30.0, pitch_deg=0.0, roll_deg=0.0, cad_scale=0.5)
    true_sim3 = Sim3(
        scale=2.5,
        rotation=_camera_to_world_rotation(orientation),
        translation=np.asarray([10.0, -3.0, 1.5]),
    )
    states = {
        int(frame): CameraState(
            camera_x=float(center[0]),
            camera_y=float(center[1]),
            camera_z=float(center[2]),
            yaw_deg=orientation.yaw_deg,
            pitch_deg=orientation.pitch_deg,
            roll_deg=orientation.roll_deg,
            cad_scale=0.5,
        )
        for frame, center in zip(traj.frames[:3], true_sim3.apply(traj.centers[:3]))
    }
    config = AlignmentConfig(cad_scale=0.5, origin_xy=(1000.0, 2000.0))

    correspondences = build_correspondences(_track_from_states(states), traj, config)
    estimated = estimate_global_sim3(correspondences)

    np.testing.assert_allclose(estimated.scale, true_sim3.scale, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(estimated.rotation, true_sim3.rotation, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(estimated.translation, true_sim3.translation, rtol=1e-6, atol=1e-6)


def test_rotation_only_alignment_keeps_manual_position_and_uses_sfm_orientation() -> None:
    traj = _trajectory()
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0))
    states = {
        0: CameraState(camera_x=8.0, camera_y=5.0, camera_z=2.0, yaw_deg=10.0, cad_scale=1.0),
        10: CameraState(camera_x=8.0, camera_y=5.0, camera_z=2.0, yaw_deg=35.0, cad_scale=1.0),
    }

    correspondences = build_correspondences(
        _track_from_states(states, origin_xy=(0.0, 0.0)),
        traj,
        config,
    )

    sim3 = estimate_global_sim3(correspondences)
    anchored = apply_segment_anchoring(sim3, correspondences, traj, config)
    middle = aligned_state_at_frame(5, traj, sim3, anchored, config)

    assert sim3.scale == pytest.approx(1.0)
    assert anchored.position_mode == "rotation_only"
    assert middle.camera_x == pytest.approx(8.0)
    assert middle.camera_y == pytest.approx(5.0)
    assert middle.camera_z == pytest.approx(2.0)


def test_estimate_global_sim3_uses_camera_orientation_for_two_anchor_twist() -> None:
    traj = _trajectory()
    orientation = CameraState(yaw_deg=0.0, pitch_deg=0.0, roll_deg=90.0, cad_scale=0.5)
    true_sim3 = Sim3(
        scale=2.5,
        rotation=_camera_to_world_rotation(orientation),
        translation=np.asarray([10.0, -3.0, 1.5]),
    )
    states = {
        int(frame): CameraState(
            camera_x=float(center[0]),
            camera_y=float(center[1]),
            camera_z=float(center[2]),
            yaw_deg=orientation.yaw_deg,
            pitch_deg=orientation.pitch_deg,
            roll_deg=orientation.roll_deg,
            cad_scale=0.5,
        )
        for frame, center in zip(traj.frames[:2], true_sim3.apply(traj.centers[:2]))
    }
    config = AlignmentConfig(cad_scale=0.5, origin_xy=(1000.0, 2000.0))

    estimated = estimate_global_sim3(build_correspondences(_track_from_states(states), traj, config))

    np.testing.assert_allclose(estimated.rotation, true_sim3.rotation, rtol=1e-6, atol=1e-6)


def test_two_anchor_sim3_maps_positions_exactly_when_orientations_conflict() -> None:
    source_centers = np.asarray(
        [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
        dtype=np.float64,
    )
    cad_centers = np.asarray(
        [[100.0, 200.0, 5.0], [100.0, 225.0, 5.0]],
        dtype=np.float64,
    )
    correspondences = [
        KeyframeCorrespondence(
            frame_index=index * 10,
            state=CameraState(
                camera_x=float(cad_center[0]),
                camera_y=float(cad_center[1]),
                camera_z=float(cad_center[2]),
                yaw_deg=0.0,
                pitch_deg=0.0,
                roll_deg=0.0,
                cad_scale=1.0,
            ),
            center_cad=cad_center,
            center_sfm=source_center,
            r_camfromworld_sfm=np.eye(3, dtype=np.float64),
            source="manual_keyframe",
        )
        for index, (source_center, cad_center) in enumerate(zip(source_centers, cad_centers))
    ]

    estimated = estimate_global_sim3(correspondences)

    np.testing.assert_allclose(estimated.apply(source_centers), cad_centers, rtol=0.0, atol=1e-6)


def test_two_anchor_sim3_uses_both_orientation_targets_for_compromise_twist() -> None:
    source_centers = np.asarray(
        [[0.0, 0.0, 0.0], [0.0, 0.0, 4.0]],
        dtype=np.float64,
    )
    cad_centers = np.asarray(
        [[10.0, 20.0, 30.0], [10.0, 28.0, 30.0]],
        dtype=np.float64,
    )
    target_rolls = (20.0, 100.0)
    correspondences = [
        KeyframeCorrespondence(
            frame_index=index * 10,
            state=CameraState(
                camera_x=float(cad_center[0]),
                camera_y=float(cad_center[1]),
                camera_z=float(cad_center[2]),
                yaw_deg=0.0,
                pitch_deg=0.0,
                roll_deg=target_rolls[index],
                cad_scale=1.0,
            ),
            center_cad=cad_center,
            center_sfm=source_center,
            r_camfromworld_sfm=np.eye(3, dtype=np.float64),
            source="manual_keyframe",
        )
        for index, (source_center, cad_center) in enumerate(zip(source_centers, cad_centers))
    ]
    expected_compromise = _camera_to_world_rotation(
        CameraState(yaw_deg=0.0, pitch_deg=0.0, roll_deg=60.0, cad_scale=1.0)
    )

    estimated = estimate_global_sim3(correspondences)

    np.testing.assert_allclose(estimated.rotation, expected_compromise, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(estimated.apply(source_centers), cad_centers, rtol=0.0, atol=1e-12)


@pytest.mark.parametrize(
    "destination_direction",
    [
        pytest.param(np.asarray([1.0, 0.0, 0.0]), id="parallel"),
        pytest.param(np.asarray([-1.0, 0.0, 0.0]), id="antiparallel"),
        pytest.param(
            np.asarray([math.cos(1e-6), math.sin(1e-6), 0.0]),
            id="near_parallel_inside_old_threshold",
        ),
        pytest.param(
            np.asarray([math.cos(math.pi - 1e-6), math.sin(math.pi - 1e-6), 0.0]),
            id="near_antiparallel_inside_old_threshold",
        ),
        pytest.param(
            np.asarray([math.cos(math.pi - 2e-6), math.sin(math.pi - 2e-6), 0.0]),
            id="near_antiparallel_outside_old_threshold",
        ),
    ],
)
def test_two_anchor_sim3_maps_rotation_boundaries_with_proper_rotation(
    destination_direction: np.ndarray,
) -> None:
    source_first = np.asarray([3.0, -4.0, 5.0])
    cad_first = np.asarray([100.0, 200.0, 5.0])
    source_centers = np.asarray(
        [source_first, source_first + np.asarray([10.0, 0.0, 0.0])],
        dtype=np.float64,
    )
    cad_centers = np.asarray(
        [cad_first, cad_first + 25.0 * destination_direction],
        dtype=np.float64,
    )
    correspondences = [
        KeyframeCorrespondence(
            frame_index=index * 10,
            state=CameraState(
                camera_x=float(cad_center[0]),
                camera_y=float(cad_center[1]),
                camera_z=float(cad_center[2]),
                cad_scale=1.0,
            ),
            center_cad=cad_center,
            center_sfm=source_center,
            r_camfromworld_sfm=np.eye(3, dtype=np.float64),
            source="manual_keyframe",
        )
        for index, (source_center, cad_center) in enumerate(zip(source_centers, cad_centers))
    ]

    estimated = estimate_global_sim3(correspondences)

    np.testing.assert_allclose(estimated.apply(source_centers), cad_centers, rtol=0.0, atol=1e-10)
    np.testing.assert_allclose(
        estimated.rotation.T @ estimated.rotation,
        np.eye(3, dtype=np.float64),
        rtol=0.0,
        atol=1e-12,
    )
    assert np.linalg.det(estimated.rotation) == pytest.approx(1.0, rel=0.0, abs=1e-12)


def test_segment_anchoring_passes_through_manual_keyframes_and_keeps_edge_residuals() -> None:
    traj = _trajectory()
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, start_frame=0, end_frame=30)
    sim3 = Sim3.identity()
    states = {
        10: CameraState(camera_x=5.0, camera_y=1.0, camera_z=0.0, yaw_deg=10.0, pitch_deg=3.0, cad_scale=1.0),
        20: CameraState(camera_x=7.0, camera_y=4.0, camera_z=2.0, yaw_deg=20.0, pitch_deg=5.0, cad_scale=1.0),
    }
    correspondences = build_correspondences(_track_from_states(states, origin_xy=(0.0, 0.0)), traj, config)

    anchored = apply_segment_anchoring(sim3, correspondences, traj, config)
    rows = generate_aligned_camera_path(traj, sim3, anchored, config)
    by_frame = {row["frame_index"]: row for row in rows}

    assert by_frame[10]["camera_x"] == states[10].camera_x
    assert by_frame[20]["camera_y"] == states[20].camera_y
    assert by_frame[0]["camera_x"] == 4.0
    assert by_frame[30]["camera_z"] == 2.0


def test_algorithm_prediction_is_not_used_as_anchor() -> None:
    traj = _trajectory()
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0))
    track = _track_from_states(
        {
            0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            20: CameraState(camera_x=1.0, camera_y=1.0, camera_z=0.0, cad_scale=1.0),
        },
        origin_xy=(0.0, 0.0),
    )
    track["keyframes"].append(
        {
            "frame": 30,
            "source": "algorithm_prediction",
            "camera": {"x": 999.0, "y": 999.0, "z": 999.0, "yaw": 0.0, "pitch": 0.0, "roll": 0.0, "fov": 70.0},
        }
    )

    correspondences = build_correspondences(track, traj, config)
    sim3 = estimate_global_sim3(correspondences)

    baseline = estimate_global_sim3(build_correspondences({"keyframes": track["keyframes"][:-1]}, traj, config))

    assert [row.frame_index for row in correspondences] == [0, 10, 20]
    np.testing.assert_allclose(sim3.rotation, baseline.rotation, atol=1e-6)
    np.testing.assert_allclose(sim3.translation, baseline.translation, atol=1e-6)
    assert sim3.scale == pytest.approx(baseline.scale)


def test_pitch_sign_uses_coordinate_conversion() -> None:
    traj = _trajectory()
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, start_frame=0, end_frame=0)
    track = {
        "keyframes": [
            {
                "frame": 0,
                "source": "manual_keyframe",
                "camera": {"x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0, "pitch": -30.0, "roll": 0.0, "fov": 70.0},
            }
        ]
    }

    correspondences = build_correspondences(track, traj, config)
    anchored = apply_segment_anchoring(Sim3.identity(), correspondences, traj, config)
    rows = generate_aligned_camera_path(traj, Sim3.identity(), anchored, config)

    assert correspondences[0].state.pitch_deg == 30.0
    assert rows[0]["pitch"] == 30.0


def test_non_identity_sfm_rotation_is_converted_to_camera_angles() -> None:
    expected = CameraState(yaw_deg=35.0, pitch_deg=12.0, roll_deg=4.0, cad_scale=1.0)
    cam_from_world = _camera_to_world_rotation(expected).T
    traj = SfmTrajectory(
        frames=np.asarray([0, 10], dtype=np.int64),
        centers=np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float64),
        quats_c2w_wxyz=np.asarray([_matrix_to_quat_wxyz(cam_from_world), _matrix_to_quat_wxyz(cam_from_world)], dtype=np.float64),
        fps=25.0,
        width=100,
        height=100,
        intrinsics={"width": 100, "params": [50.0]},
    )
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), fov_from="config")

    state = aligned_state_at_frame(5, traj, Sim3.identity(), anchored=None, config=config)

    assert state.yaw_deg == pytest.approx(expected.yaw_deg, abs=1e-6)
    assert state.pitch_deg == pytest.approx(expected.pitch_deg, abs=1e-6)
    assert state.roll_deg == pytest.approx(expected.roll_deg, abs=1e-6)


def test_path_rows_and_camera_track_prediction_schema(tmp_path: Path) -> None:
    traj = _trajectory()
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, frontend_track_step=10)
    states = {
        0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
        10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
    }
    track = _track_from_states(states, origin_xy=(0.0, 0.0))
    track["keyframes"][1]["source"] = "confirmed_keyframe"
    correspondences = build_correspondences(track, traj, config)
    anchored = apply_segment_anchoring(Sim3.identity(), correspondences, traj, config)
    rows = generate_aligned_camera_path(traj, Sim3.identity(), anchored, config)
    pred = generate_camera_track_pred(track, rows, config)

    required = {"frame_index", "camera_x", "camera_y", "camera_z", "yaw", "pitch", "roll", "fov", "path_source", "status"}
    assert required.issubset(rows[0].keys())
    write_csv_utf8_sig(tmp_path / "sfm_camera_path.csv", rows)
    assert (tmp_path / "sfm_camera_path.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    sources = {row["source"] for row in pred["keyframes"]}
    assert {"manual_keyframe", "confirmed_keyframe", "algorithm_prediction"}.issubset(sources)
    assert any(row["frame"] == 20 and row["source"] == "algorithm_prediction" for row in pred["keyframes"])
    assert pred["meta"] == {
        "generated_by": "cadscene.align_to_cad",
        "coordinate_system": "web_cad_world",
        "manual_keyframes_preserved": True,
        "algorithm_prediction_step": 10,
    }
    json.dumps(pred, ensure_ascii=False)


def test_manual_fov_overrides_trajectory_for_path_and_predictions(tmp_path: Path) -> None:
    traj = _trajectory()
    traj.intrinsics["params"] = [3788.0]
    track = _track_from_states(
        {
            0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
        },
        origin_xy=(0.0, 0.0),
    )
    track["keyframes"][0]["camera"]["fov"] = 67.0
    track["keyframes"][1]["source"] = "confirmed_keyframe"
    track["keyframes"][1]["camera"]["fov"] = 67.0
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, frontend_track_step=10),
    )

    assert traj.horizontal_fov_deg() == pytest.approx(28.414, abs=0.1)
    assert {row["fov"] for row in result.sfm_camera_path_rows} == {67.0}
    predicted = [row for row in result.camera_track_pred["keyframes"] if row["source"] == "algorithm_prediction"]
    assert predicted
    assert {row["camera"]["fov"] for row in predicted} == {67.0}
    assert result.camera_track_pred["keyframes"][:2] == track["keyframes"]


def test_inconsistent_manual_fov_keeps_trajectory_fallback(tmp_path: Path) -> None:
    traj = _trajectory()
    traj.intrinsics["params"] = [3788.0]
    track = _track_from_states(
        {
            0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
        },
        origin_xy=(0.0, 0.0),
    )
    track["keyframes"][0]["camera"]["fov"] = 67.0
    track["keyframes"][1]["camera"]["fov"] = 68.0
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, frontend_track_step=10),
    )

    fallback_fov = traj.horizontal_fov_deg()
    assert fallback_fov is not None
    assert all(row["fov"] == pytest.approx(fallback_fov) for row in result.sfm_camera_path_rows)
    predicted = [row for row in result.camera_track_pred["keyframes"] if row["source"] == "algorithm_prediction"]
    assert predicted
    assert all(row["camera"]["fov"] == pytest.approx(fallback_fov) for row in predicted)
