from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from cadscene.alignment import aligner
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


def _write_trajectory(
    path: Path, traj: SfmTrajectory, *, meta: dict | None = None
) -> None:
    path.write_text(
        json.dumps(
            {
                "fps": traj.fps,
                "width": traj.width,
                "height": traj.height,
                "intrinsics": [traj.intrinsics],
                "meta": meta or {},
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


def _write_metric_full_pose_trajectory(path: Path) -> SfmTrajectory:
    trajectory = _trajectory()
    _write_trajectory(
        path,
        trajectory,
        meta={
            "trajectory_mode": "srt_full_pose",
            "coordinate_system": "cad_local_m",
            "metric_scale_locked": True,
        },
    )
    return trajectory


def _write_position_only_fixed_track(path: Path) -> np.ndarray:
    centers = np.asarray(
        [
            [0.0, 0.0, 80.0],
            [2.0, 0.0, 80.2],
            [3.0, 1.5, 80.5],
            [4.0, 3.0, 80.6],
        ],
        dtype=np.float64,
    )
    path.write_text(
        json.dumps(
            {
                "fps": 20.0,
                "width": 1920,
                "height": 1080,
                "intrinsics": [
                    {
                        "model": "PINHOLE",
                        "width": 1920,
                        "height": 1080,
                        "params": [960.0, 960.0, 960.0, 540.0],
                    }
                ],
                "poses": [
                    {
                        "frame_index": frame,
                        "registered": False,
                        "position_available": True,
                        "orientation_available": False,
                        "center": center.tolist(),
                    }
                    for frame, center in zip((0, 10, 20, 30), centers)
                ],
                "meta": {
                    "trajectory_mode": "srt_fixed_track_visual_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                    "position_source": "srt_cad_locked",
                },
            }
        ),
        encoding="utf-8",
    )
    return centers


def _write_relative_fixed_track(path: Path) -> tuple[np.ndarray, dict[int, np.ndarray]]:
    centers = np.asarray(
        [
            [0.0, 0.0, 80.0],
            [2.0, 0.0, 80.2],
            [3.0, 1.5, 80.5],
            [4.0, 3.0, 80.6],
        ],
        dtype=np.float64,
    )
    local_rotations = {
        frame: Rotation.from_euler("z", angle, degrees=True).as_matrix()
        for frame, angle in zip((0, 10, 20, 30), (0.0, 10.0, 20.0, 30.0))
    }
    path.write_text(
        json.dumps(
            {
                "fps": 20.0,
                "width": 1920,
                "height": 1080,
                "intrinsics": [
                    {
                        "model": "PINHOLE",
                        "width": 1920,
                        "height": 1080,
                        "params": [960.0, 960.0, 960.0, 540.0],
                    }
                ],
                "poses": [
                    {
                        "frame_index": frame,
                        "registered": False,
                        "position_available": True,
                        "orientation_available": False,
                        "center": center.tolist(),
                        "visual_component_id": 0,
                        "cam_from_visual_local_quat_wxyz": _matrix_to_quat_wxyz(
                            local_rotations[frame]
                        ),
                    }
                    for frame, center in zip((0, 10, 20, 30), centers)
                ],
                "meta": {
                    "trajectory_mode": "srt_fixed_track_visual_pose",
                    "coordinate_system": "cad_local_m",
                    "metric_scale_locked": True,
                    "position_source": "srt_cad_locked",
                    "recommended_anchor_frame": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    return centers, local_rotations


def _state_from_world_rotation(
    center: np.ndarray, world_from_camera: np.ndarray
) -> CameraState:
    yaw, pitch, roll = aligner._decompose_world_from_cam(world_from_camera)
    return CameraState(
        camera_x=float(center[0]),
        camera_y=float(center[1]),
        camera_z=float(center[2]),
        yaw_deg=yaw,
        pitch_deg=pitch,
        roll_deg=roll,
        fov_deg=72.0,
    )


def _row_world_rotation(row: dict) -> np.ndarray:
    return _camera_to_world_rotation(CameraState.from_row(row, cad_scale=1.0))


def test_fixed_track_one_anchor_propagates_relative_orientation_and_keeps_xyz(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "relative-fixed-track.json"
    centers, local_rotations = _write_relative_fixed_track(trajectory_path)
    manual_world_from_camera = _camera_to_world_rotation(
        CameraState(yaw_deg=42.0, pitch_deg=-18.0, roll_deg=3.0)
    )
    anchor = _state_from_world_rotation(centers[1], manual_world_from_camera)
    track_path = tmp_path / "manual-track.json"
    track_path.write_text(
        json.dumps(_track_from_states({10: anchor}, origin_xy=(0.0, 0.0))),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
            frontend_track_step=10,
        ),
    )

    actual_centers = np.asarray(
        [
            [row["camera_x"], row["camera_y"], row["camera_z"]]
            for row in result.sfm_camera_path_rows
        ]
    )
    np.testing.assert_allclose(actual_centers, centers, atol=1e-12)
    assert all(row["orientation_available"] for row in result.sfm_camera_path_rows)
    rows = {row["frame_index"]: row for row in result.sfm_camera_path_rows}
    np.testing.assert_allclose(
        _row_world_rotation(rows[10]), manual_world_from_camera, atol=1e-8
    )
    world_from_local = manual_world_from_camera @ local_rotations[10]
    np.testing.assert_allclose(
        _row_world_rotation(rows[20]),
        world_from_local @ local_rotations[20].T,
        atol=1e-8,
    )
    assert result.alignment_json["validation"]["orientation_anchor_count"] == 1
    assert result.alignment_json["validation"]["drift_corrected"] is False


def test_fixed_track_second_anchor_corrects_large_orientation_drift(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "relative-fixed-track.json"
    centers, local_rotations = _write_relative_fixed_track(trajectory_path)
    first_world = _camera_to_world_rotation(
        CameraState(yaw_deg=35.0, pitch_deg=-25.0, roll_deg=2.0)
    )
    first_correction = first_world @ local_rotations[0]
    second_correction = (
        Rotation.from_euler("z", 12.0, degrees=True).as_matrix()
        @ first_correction
    )
    second_world = second_correction @ local_rotations[30].T
    track_path = tmp_path / "manual-track.json"
    track_path.write_text(
        json.dumps(
            _track_from_states(
                {
                    0: _state_from_world_rotation(centers[0], first_world),
                    30: _state_from_world_rotation(centers[3], second_world),
                },
                origin_xy=(0.0, 0.0),
            )
        ),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
            frontend_track_step=10,
        ),
    )

    rows = {row["frame_index"]: row for row in result.sfm_camera_path_rows}
    np.testing.assert_allclose(_row_world_rotation(rows[0]), first_world, atol=1e-8)
    np.testing.assert_allclose(_row_world_rotation(rows[30]), second_world, atol=1e-8)
    validation = result.alignment_json["validation"]
    assert validation["orientation_anchor_count"] == 2
    assert validation["orientation_anchor_residual_max_deg"] == pytest.approx(12.0)
    assert validation["drift_corrected"] is True


def test_fixed_track_alignment_keeps_centers_plus_one_translation_and_slerps_attitude(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "fixed-track.json"
    centers = _write_position_only_fixed_track(trajectory_path)
    offset = np.asarray([2.0, -3.0, 4.0])
    track = _track_from_states(
        {
            0: CameraState(
                camera_x=2.0,
                camera_y=-3.0,
                camera_z=84.0,
                yaw_deg=170.0,
                pitch_deg=-20.0,
                fov_deg=72.0,
            ),
            20: CameraState(
                camera_x=float(centers[2, 0] + offset[0]),
                camera_y=float(centers[2, 1] + offset[1]),
                camera_z=float(centers[2, 2] + offset[2]),
                yaw_deg=-170.0,
                pitch_deg=-20.0,
                fov_deg=72.0,
            ),
        },
        origin_xy=(0.0, 0.0),
    )
    track_path = tmp_path / "manual-track.json"
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
            frontend_track_step=10,
        ),
    )

    actual_centers = np.asarray(
        [
            [row["camera_x"], row["camera_y"], row["camera_z"]]
            for row in result.sfm_camera_path_rows
        ]
    )
    np.testing.assert_allclose(actual_centers, centers + offset, atol=1e-12)
    midpoint = next(row for row in result.sfm_camera_path_rows if row["frame_index"] == 10)
    assert abs(abs(midpoint["yaw"]) - 180.0) < 1.0
    assert result.alignment_json["sim3"]["scale"] == 1.0
    np.testing.assert_allclose(
        result.alignment_json["sim3"]["rotation"], np.eye(3), atol=1e-12
    )
    assert result.alignment_json["validation"]["position_source"] == (
        "srt_cad_locked"
    )
    assert result.metrics["alignment_mode"] == "srt_fixed_track"


def test_fixed_track_alignment_rejects_nonuniform_position_edits(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "fixed-track.json"
    centers = _write_position_only_fixed_track(trajectory_path)
    first = CameraState(camera_x=1.0, camera_y=0.0, camera_z=80.0, fov_deg=72.0)
    second = CameraState(
        camera_x=float(centers[2, 0] + 2.0),
        camera_y=float(centers[2, 1]),
        camera_z=float(centers[2, 2]),
        fov_deg=72.0,
    )
    track_path = tmp_path / "nonuniform-track.json"
    track_path.write_text(
        json.dumps(
            _track_from_states(
                {0: first, 20: second}, origin_xy=(0.0, 0.0)
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="one whole-route XYZ offset"):
        run_alignment(
            trajectory_path=trajectory_path,
            web_camera_track_path=track_path,
            config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
        )


def test_metric_full_pose_alignment_keeps_scale_one_with_single_manual_anchor(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "full_pose.json"
    trajectory = _write_metric_full_pose_trajectory(trajectory_path)
    source_center = trajectory.centers[0]
    corrected = CameraState(
        camera_x=float(source_center[0] + 4.0),
        camera_y=float(source_center[1] - 3.0),
        camera_z=float(source_center[2] + 1.5),
        yaw_deg=8.0,
        pitch_deg=2.0,
        roll_deg=-1.0,
        fov_deg=67.0,
        cad_scale=1.0,
    )
    track_path = tmp_path / "camera_track.json"
    track_path.write_text(
        json.dumps(
            _track_from_states({0: corrected}, origin_xy=(0.0, 0.0))
        ),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
        ),
    )

    assert result.alignment_json["sim3"]["scale"] == pytest.approx(1.0)
    np.testing.assert_allclose(
        result.alignment_json["sim3"]["rotation"], np.eye(3)
    )
    assert result.alignment_json["sim3"]["translation"] == pytest.approx(
        [4.0, -3.0, 1.5]
    )
    assert result.alignment_json["validation"]["alignment_mode"] == "metric_direct"
    assert result.alignment_json["validation"]["metric_scale_locked"] is True


def test_metric_full_pose_alignment_applies_authoritative_uniform_route_offset(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "full_pose.json"
    trajectory = _write_metric_full_pose_trajectory(trajectory_path)
    offset = np.asarray([4.0, -3.0, 1.5], dtype=np.float64)
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10)
    states = {}
    for frame in trajectory.frames:
        original = aligned_state_at_frame(
            int(frame), trajectory, Sim3.identity(), None, config
        )
        states[int(frame)] = replace(
            original,
            camera_x=original.camera_x + float(offset[0]),
            camera_y=original.camera_y + float(offset[1]),
            camera_z=original.camera_z + float(offset[2]),
        )
    track = _track_from_states(states, origin_xy=(0.0, 0.0))
    for keyframe in track["keyframes"]:
        keyframe["source"] = "srt_full_pose"
    track["meta"] = {
        "workflow": "srt_full_pose",
        "position_edit_policy": "uniform_xyz_offset_only",
        "authoritative_workbench_track": True,
        "route_offset_xyz_m": offset.tolist(),
    }
    track_path = tmp_path / "camera_track.json"
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=config,
    )

    assert result.alignment_json["sim3"]["scale"] == 1.0
    assert result.alignment_json["sim3"]["translation"] == pytest.approx(
        offset.tolist()
    )
    assert result.alignment_json["validation"]["alignment_mode"] == "metric_direct"


def test_metric_full_pose_rejects_non_uniform_authoritative_track_edits(
    tmp_path: Path,
) -> None:
    trajectory_path = tmp_path / "full_pose.json"
    trajectory = _write_metric_full_pose_trajectory(trajectory_path)
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10)
    states = {}
    for frame in trajectory.frames:
        original = aligned_state_at_frame(
            int(frame), trajectory, Sim3.identity(), None, config
        )
        states[int(frame)] = replace(original, camera_x=original.camera_x + 2.0)
    track = _track_from_states(states, origin_xy=(0.0, 0.0))
    for keyframe in track["keyframes"]:
        keyframe["source"] = "srt_full_pose"
    track["keyframes"][-1]["camera"]["x"] += 0.2
    track["meta"] = {
        "workflow": "srt_full_pose",
        "position_edit_policy": "uniform_xyz_offset_only",
        "authoritative_workbench_track": True,
        "route_offset_xyz_m": [2.0, 0.0, 0.0],
    }
    track_path = tmp_path / "camera_track.json"
    track_path.write_text(json.dumps(track), encoding="utf-8")

    with pytest.raises(RuntimeError, match="uniform XYZ offset"):
        run_alignment(
            trajectory_path=trajectory_path,
            web_camera_track_path=track_path,
            config=config,
        )


@pytest.mark.parametrize(
    ("field", "delta", "message"),
    (("yaw", 1.0, "attitude"), ("fov", 1.0, "FOV")),
)
def test_metric_full_pose_rejects_authoritative_attitude_or_fov_edits(
    tmp_path: Path,
    field: str,
    delta: float,
    message: str,
) -> None:
    trajectory_path = tmp_path / "full_pose.json"
    trajectory = _write_metric_full_pose_trajectory(trajectory_path)
    trajectory_payload = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory_payload["meta"]["horizontal_fov_deg"] = 67.0
    trajectory_payload["intrinsics"][0]["params"][0] = (
        0.5 * trajectory.width / math.tan(math.radians(67.0) * 0.5)
    )
    trajectory_path.write_text(json.dumps(trajectory_payload), encoding="utf-8")
    config = AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10)
    states = {}
    for frame in trajectory.frames:
        original = aligned_state_at_frame(
            int(frame), trajectory, Sim3.identity(), None, config
        )
        states[int(frame)] = replace(
            original,
            camera_x=original.camera_x + 2.0,
            fov_deg=67.0,
        )
    track = _track_from_states(states, origin_xy=(0.0, 0.0))
    for keyframe in track["keyframes"]:
        keyframe["source"] = "srt_full_pose"
        keyframe["camera"][field] += delta
    track["meta"] = {
        "workflow": "srt_full_pose",
        "position_edit_policy": "uniform_xyz_offset_only",
        "authoritative_workbench_track": True,
        "route_offset_xyz_m": [2.0, 0.0, 0.0],
    }
    track_path = tmp_path / "camera_track.json"
    track_path.write_text(json.dumps(track), encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        run_alignment(
            trajectory_path=trajectory_path,
            web_camera_track_path=track_path,
            config=config,
        )


def test_metric_full_pose_never_calls_free_sim3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trajectory_path = tmp_path / "full_pose.json"
    _write_metric_full_pose_trajectory(trajectory_path)
    track_path = tmp_path / "camera_track.json"
    track_path.write_text(json.dumps({"keyframes": []}), encoding="utf-8")

    def fail_free_sim3(_correspondences):
        raise AssertionError("free Sim3 must not run for metric full-pose input")

    monkeypatch.setattr(aligner, "estimate_global_sim3", fail_free_sim3)

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0)),
    )

    assert result.alignment_json["sim3"]["scale"] == 1.0
    assert result.metrics["alignment_mode"] == "metric_direct"


def test_metric_full_pose_alignment_preserves_unregistered_frame_gaps() -> None:
    trajectory = SfmTrajectory(
        frames=np.asarray([0, 3], dtype=np.int64),
        centers=np.asarray(
            [[0.0, 0.0, 10.0], [3.0, 0.0, 10.0]], dtype=np.float64
        ),
        quats_c2w_wxyz=np.asarray(
            [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
            dtype=np.float64,
        ),
        fps=25.0,
        width=1920,
        height=1080,
        intrinsics={"width": 1920, "params": [960.0]},
        meta={
            "trajectory_mode": "srt_full_pose",
            "coordinate_system": "cad_local_m",
            "metric_scale_locked": True,
        },
        unregistered_frames=np.asarray([1, 2], dtype=np.int64),
    )
    config = AlignmentConfig(
        cad_scale=1.0,
        origin_xy=(0.0, 0.0),
        frontend_track_step=1,
        frame_step=3,
    )
    rows = generate_aligned_camera_path(
        trajectory, Sim3.identity(), anchored=None, config=config
    )
    prediction = generate_camera_track_pred({"keyframes": []}, rows, config)

    assert [row["status"] for row in rows] == [
        "ok",
        "unregistered",
        "unregistered",
        "ok",
    ]
    assert [item["frame"] for item in prediction["keyframes"]] == [0, 3]


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


def test_run_alignment_allows_large_global_residual_when_scale_is_unobservable(tmp_path: Path) -> None:
    base_traj = _trajectory()
    traj = replace(base_traj, centers=base_traj.centers * 20.0)
    coincident = CameraState(
        camera_x=8.0,
        camera_y=5.0,
        camera_z=2.0,
        yaw_deg=10.0,
        fov_deg=67.0,
        cad_scale=1.0,
    )
    track = _track_from_states(
        {0: coincident, 10: coincident},
        origin_xy=(0.0, 0.0),
    )
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
        ),
    )

    assert result.metrics["alignment_mode"] == "rotation_only"
    assert result.metrics["scale_observable"] is False
    assert result.metrics["anchored_residual_m_max"] == pytest.approx(0.0)
    assert result.metrics["global_residual_m_max"] > aligner.MAX_GLOBAL_ANCHOR_RESIDUAL_M


def test_run_alignment_skips_baseline_direction_for_submicron_cad_separation(
    tmp_path: Path,
) -> None:
    traj = _trajectory()
    states = {
        0: CameraState(
            camera_x=8.0,
            camera_y=5.0,
            camera_z=2.0,
            fov_deg=67.0,
            cad_scale=1.0,
        ),
        10: CameraState(
            camera_x=8.0,
            camera_y=5.0000005,
            camera_z=2.0,
            fov_deg=67.0,
            cad_scale=1.0,
        ),
    }
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(
        json.dumps(_track_from_states(states, origin_xy=(0.0, 0.0))),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
        ),
    )

    assert result.metrics["alignment_mode"] == "rotation_only"
    assert result.metrics["scale_observable"] is False
    assert "baseline_direction_error_deg" not in result.metrics


def test_run_alignment_records_baseline_direction_for_small_sfm_units(
    tmp_path: Path,
) -> None:
    traj = SfmTrajectory(
        frames=np.asarray([0, 10], dtype=np.int64),
        centers=np.asarray(
            [[0.0, 0.0, 0.0], [5e-7, 0.0, 0.0]],
            dtype=np.float64,
        ),
        quats_c2w_wxyz=np.asarray(
            [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
            dtype=np.float64,
        ),
        fps=25.0,
        width=1920,
        height=1080,
        intrinsics={"width": 1920, "params": [960.0]},
    )
    states = {
        0: CameraState(
            camera_x=0.0,
            camera_y=0.0,
            camera_z=0.0,
            fov_deg=67.0,
            cad_scale=1.0,
        ),
        10: CameraState(
            camera_x=0.0,
            camera_y=1.0,
            camera_z=0.0,
            fov_deg=67.0,
            cad_scale=1.0,
        ),
    }
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(
        json.dumps(_track_from_states(states, origin_xy=(0.0, 0.0))),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
        ),
    )

    assert result.metrics["alignment_mode"] == "sfm_residual"
    assert result.metrics["scale_observable"] is True
    assert result.metrics["baseline_direction_error_deg"] == pytest.approx(
        0.0,
        abs=1e-9,
    )


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


def test_two_anchor_sim3_treats_non_axis_antiparallel_roundoff_as_collinear() -> None:
    direction = np.asarray([0.7, -0.4, 0.6], dtype=np.float64)
    direction /= np.linalg.norm(direction)
    source_first = np.asarray([1000.0, -1000.0, 500.0])
    source_centers = np.asarray(
        [source_first, source_first + 10.0 * direction],
        dtype=np.float64,
    )
    cad_first = np.asarray([-700.0, 300.0, 1200.0])
    cad_centers = np.asarray(
        [cad_first, cad_first - 25.0 * direction],
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


def test_two_anchor_sim3_preserves_genuine_pi_minus_1e8_direction_on_250m_baseline() -> None:
    angle = math.pi - 1e-8
    source_first = np.asarray([1000.0, -1000.0, 500.0], dtype=np.float64)
    source_centers = np.asarray(
        [source_first, source_first + np.asarray([250.0, 0.0, 0.0])],
        dtype=np.float64,
    )
    cad_first = np.asarray([-700.0, 300.0, 1200.0], dtype=np.float64)
    destination_direction = np.asarray(
        [math.cos(angle), math.sin(angle), 0.0],
        dtype=np.float64,
    )
    cad_centers = np.asarray(
        [cad_first, cad_first + 250.0 * destination_direction],
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
    endpoint_error = float(
        np.max(np.linalg.norm(estimated.apply(source_centers) - cad_centers, axis=1))
    )

    assert endpoint_error < 1e-6
    np.testing.assert_allclose(
        estimated.rotation.T @ estimated.rotation,
        np.eye(3, dtype=np.float64),
        rtol=0.0,
        atol=1e-12,
    )
    assert np.linalg.det(estimated.rotation) == pytest.approx(1.0, rel=0.0, abs=1e-12)


@pytest.mark.parametrize(
    "delta",
    [
        pytest.param(1e-8, id="inside_sqrt_eps"),
        pytest.param(1.5e-8, id="just_above_sqrt_eps"),
    ],
)
def test_two_anchor_sim3_preserves_translated_non_axis_near_antiparallel_direction(
    delta: float,
) -> None:
    direction = np.asarray([0.7, -0.4, 0.6], dtype=np.float64)
    direction /= np.linalg.norm(direction)
    perpendicular = np.asarray([1.0, 0.0, -3.0], dtype=np.float64)
    perpendicular -= direction * float(np.dot(direction, perpendicular))
    perpendicular /= np.linalg.norm(perpendicular)
    destination_direction = (
        -math.cos(delta) * direction + math.sin(delta) * perpendicular
    )
    source_first = np.asarray([1000.0, -1000.0, 500.0], dtype=np.float64)
    source_centers = np.asarray(
        [source_first, source_first + 10.0 * direction],
        dtype=np.float64,
    )
    cad_first = np.asarray([-700.0, 300.0, 1200.0], dtype=np.float64)
    cad_centers = np.asarray(
        [cad_first, cad_first + 250.0 * destination_direction],
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
    endpoint_error = float(
        np.max(np.linalg.norm(estimated.apply(source_centers) - cad_centers, axis=1))
    )

    assert endpoint_error < 1e-8
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


def test_run_alignment_normalizes_prediction_times_to_trajectory_fps(tmp_path: Path) -> None:
    traj = _trajectory()
    track = _track_from_states(
        {
            0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
        },
        origin_xy=(0.0, 0.0),
    )
    track["fps"] = 23.976
    for keyframe in track["keyframes"]:
        keyframe["time"] = keyframe["frame"] / 23.976
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=10,
            frontend_track_step=10,
        ),
    )

    assert result.camera_track_pred["fps"] == pytest.approx(traj.fps)
    assert result.camera_track_pred["keyframes"]
    for keyframe in result.camera_track_pred["keyframes"]:
        assert keyframe["time"] == pytest.approx(keyframe["frame"] / traj.fps)


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
    preserved = [
        {key: value for key, value in keyframe.items() if key != "time"}
        for keyframe in result.camera_track_pred["keyframes"][:2]
    ]
    assert preserved == track["keyframes"]
    assert [keyframe["time"] for keyframe in result.camera_track_pred["keyframes"][:2]] == pytest.approx(
        [0.0, 10.0 / traj.fps]
    )


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


def test_alignment_allows_global_anchor_residual_below_ten_metres() -> None:
    aligner._validate_alignment_result(
        metrics={"global_residual_m_max": 5.15095666564},
        intrinsics_warning=None,
        trusted_fov=True,
    )


def test_alignment_rejects_global_anchor_residual_above_ten_metres() -> None:
    with pytest.raises(RuntimeError, match="global anchor residual"):
        aligner._validate_alignment_result(
            metrics={"global_residual_m_max": 10.01},
            intrinsics_warning=None,
            trusted_fov=True,
        )


def test_alignment_rejects_large_global_residual_with_three_keyframes() -> None:
    with pytest.raises(RuntimeError, match="global anchor residual"):
        aligner._validate_alignment_result(
            metrics={"global_residual_m_max": 45.693, "num_keyframes": 3},
            intrinsics_warning=None,
            trusted_fov=True,
        )


@pytest.mark.parametrize(
    "baseline_direction_error",
    [float("nan"), 0.1000001],
)
def test_alignment_rejects_invalid_baseline_direction_metric(
    baseline_direction_error: float,
) -> None:
    with pytest.raises(RuntimeError, match="baseline direction"):
        aligner._validate_alignment_result(
            metrics={
                "global_residual_m_max": 0.0,
                "scale_observable": True,
                "baseline_direction_error_deg": baseline_direction_error,
            },
            intrinsics_warning=None,
            trusted_fov=True,
        )


def test_real_target_shape_records_valid_baseline_direction_metric(tmp_path: Path) -> None:
    source_centers = np.asarray(
        [
            [-1.6477624556206925, -0.47592717968842735, -3.3472404058347225],
            [-1.4324010200845052, -0.568483450621065, -2.542810290320703],
        ],
        dtype=np.float64,
    )
    cad_centers = np.asarray(
        [
            [2233.8075259856414, 1147.3629063987173, 180.0],
            [2012.021886261995, 1036.0974717158824, 180.0],
        ],
        dtype=np.float64,
    )
    traj = SfmTrajectory(
        frames=np.asarray([0, 750], dtype=np.int64),
        centers=source_centers,
        quats_c2w_wxyz=np.asarray(
            [[1.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]],
            dtype=np.float64,
        ),
        fps=25.0,
        width=1920,
        height=1080,
        intrinsics={"width": 1920, "params": [960.0]},
    )
    states = {
        int(frame): CameraState(
            camera_x=float(center[0]),
            camera_y=float(center[1]),
            camera_z=float(center[2]),
            yaw_deg=-119.0,
            pitch_deg=14.0,
            fov_deg=67.0,
            cad_scale=1.0,
        )
        for frame, center in zip(traj.frames, cad_centers)
    }
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(
        json.dumps(_track_from_states(states, origin_xy=(0.0, 0.0))),
        encoding="utf-8",
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            frame_step=750,
        ),
    )

    assert result.metrics["baseline_direction_error_deg"] <= aligner.MAX_BASELINE_DIRECTION_ERROR_DEG
    assert result.alignment_json["metrics"]["baseline_direction_error_deg"] <= 0.1


@pytest.mark.parametrize("configured_fov", [float("nan"), 0.0, 180.0])
def test_run_alignment_rejects_invalid_configured_fov_before_generating_rows(
    tmp_path: Path,
    configured_fov: float,
) -> None:
    traj = _trajectory()
    track = _track_from_states(
        {
            0: CameraState(
                camera_x=0.0,
                camera_y=0.0,
                camera_z=0.0,
                fov_deg=67.0,
                cad_scale=1.0,
            ),
            10: CameraState(
                camera_x=1.0,
                camera_y=0.0,
                camera_z=0.0,
                fov_deg=68.0,
                cad_scale=1.0,
            ),
        },
        origin_xy=(0.0, 0.0),
    )
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    with pytest.raises(RuntimeError, match="configured FOV"):
        run_alignment(
            trajectory_path=trajectory_path,
            web_camera_track_path=track_path,
            config=AlignmentConfig(
                cad_scale=1.0,
                origin_xy=(0.0, 0.0),
                fov=configured_fov,
                fov_from="config",
                frame_step=10,
            ),
        )


def test_pathological_intrinsics_are_allowed_with_trusted_fov() -> None:
    aligner._validate_alignment_result(
        metrics={"global_residual_m_max": 0.0},
        intrinsics_warning="pathological intrinsics: focal ratio 9.24",
        trusted_fov=True,
    )


def test_pathological_intrinsics_are_rejected_without_trusted_fov() -> None:
    with pytest.raises(RuntimeError, match="pathological intrinsics"):
        aligner._validate_alignment_result(
            metrics={"global_residual_m_max": 0.0},
            intrinsics_warning="pathological intrinsics: focal ratio 9.24",
            trusted_fov=False,
        )


def test_pinhole_focal_aspect_ratio_is_validated() -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "PINHOLE", "params": [960.0, 960.0 * 9.24]})

    warning = aligner._trajectory_intrinsics_warning(traj)

    assert warning is not None
    assert "pathological intrinsics" in warning
    assert "9.24" in warning


def test_fov_focal_aspect_ratio_warns_and_is_rejected_without_trusted_fov() -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "FOV", "params": [960.0, 960.0 * 9.24, 960.0, 540.0, 0.5]})

    warning = aligner._trajectory_intrinsics_warning(traj)

    assert warning is not None
    assert "pathological intrinsics" in warning
    assert "9.24" in warning
    with pytest.raises(RuntimeError, match="9.24"):
        aligner._validate_alignment_result(
            metrics={"global_residual_m_max": 0.0},
            intrinsics_warning=warning,
            trusted_fov=False,
        )


@pytest.mark.parametrize(
    "model",
    [
        "PINHOLE",
        "OPENCV",
        "OPENCV_FISHEYE",
        "FULL_OPENCV",
        "FOV",
        "THIN_PRISM_FISHEYE",
        "RAD_TAN_THIN_PRISM_FISHEYE",
        "DIVISION",
        "FISHEYE",
        "EUCM",
    ],
)
def test_all_supported_two_focal_colmap_models_are_validated(model: str) -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": model, "params": [960.0, 960.0 * 9.24]})

    warning = aligner._trajectory_intrinsics_warning(traj)

    assert warning is not None
    assert "9.24" in warning


def test_intrinsics_warning_retains_ratio_precision_above_boundary() -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "FOV", "params": [1.0, 2.0001, 0.0, 0.0, 0.5]})

    warning = aligner._trajectory_intrinsics_warning(traj)

    assert warning is not None
    assert "2.0001" in warning


def test_global_residual_error_retains_precision_above_boundary() -> None:
    with pytest.raises(RuntimeError, match="10.0000001"):
        aligner._validate_alignment_result(
            metrics={"global_residual_m_max": 10.0000001},
            intrinsics_warning=None,
            trusted_fov=True,
        )


def test_manual_fov_records_pathological_intrinsics_warning(tmp_path: Path) -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "OPENCV", "params": [960.0, 960.0 * 9.24]})
    track = _track_from_states(
        {
            0: CameraState(camera_x=0.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
            10: CameraState(camera_x=1.0, camera_y=0.0, camera_z=0.0, cad_scale=1.0),
        },
        origin_xy=(0.0, 0.0),
    )
    for keyframe in track["keyframes"]:
        keyframe["camera"]["fov"] = 67.0
    track["keyframes"][1]["source"] = "confirmed_keyframe"
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=AlignmentConfig(cad_scale=1.0, origin_xy=(0.0, 0.0), frame_step=10, frontend_track_step=10),
    )

    validation = result.alignment_json["validation"]
    assert validation["status"] == "warning"
    assert validation["fov_source"] == "manual"
    assert "pathological intrinsics" in validation["intrinsics_warning"]
    assert "9.24" in validation["intrinsics_warning"]


def test_alignment_report_surfaces_manual_fov_upstream_warning(tmp_path: Path) -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "OPENCV", "params": [960.0, 960.0 * 9.24]})
    track = _track_from_states(
        {
            0: CameraState(
                camera_x=0.0,
                camera_y=0.0,
                camera_z=0.0,
                fov_deg=67.0,
                cad_scale=1.0,
            ),
            10: CameraState(
                camera_x=1.0,
                camera_y=0.0,
                camera_z=0.0,
                fov_deg=67.0,
                cad_scale=1.0,
            ),
        },
        origin_xy=(0.0, 0.0),
    )
    trajectory_path = tmp_path / "trajectory.json"
    track_path = tmp_path / "camera_track.json"
    _write_trajectory(trajectory_path, traj)
    track_path.write_text(json.dumps(track), encoding="utf-8")
    config = AlignmentConfig(
        cad_scale=1.0,
        origin_xy=(0.0, 0.0),
        frame_step=10,
    )

    result = run_alignment(
        trajectory_path=trajectory_path,
        web_camera_track_path=track_path,
        config=config,
    )
    report = aligner.build_alignment_report(
        result.metrics,
        config,
        validation=result.alignment_json["validation"],
    )

    assert aligner.UPSTREAM_SFM_MANUAL_FOV_WARNING in report
    assert "pathological intrinsics" in report
    assert "manual" in report


def test_config_fov_records_pathological_intrinsics_warning(tmp_path: Path) -> None:
    traj = _trajectory()
    traj.intrinsics.update({"model": "OPENCV", "params": [960.0, 960.0 * 9.24]})
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
        config=AlignmentConfig(
            cad_scale=1.0,
            origin_xy=(0.0, 0.0),
            fov=67.0,
            fov_from="config",
            frame_step=10,
            frontend_track_step=10,
        ),
    )

    validation = result.alignment_json["validation"]
    assert validation["status"] == "warning"
    assert validation["fov_source"] == "config"
    assert "pathological intrinsics" in validation["intrinsics_warning"]
