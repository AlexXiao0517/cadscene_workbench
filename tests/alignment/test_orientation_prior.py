from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from cadscene.alignment.aligner import _camera_to_world_rotation
from cadscene.alignment.keyframe_schema import normalize_camera_track
from cadscene.alignment.orientation_prior import (
    PriorQualificationConfig,
    build_orientation_prior,
    qualify_orientation_prior,
    validate_prior_split,
)
from cadscene.core.coordinates import web_camera_to_python_state


def _keyframe(*, frame=100, direction_type="camera_forward", role="solve", source="manual_anchor", confirmed=True, roll_confirmed=False, yaw=45.0, pitch=-20.0):
    return {
        "frame": frame, "source_frame_index": frame, "time": frame / 30.0, "pts_time_sec": frame / 30.0,
        "source": source,
        "camera": {"x": 110.0, "y": 220.0, "z": 30.0, "yaw": yaw, "pitch": pitch, "roll": 15.0, "fov": 75.0},
        "orientation_metadata": {"orientation_source": "manual", "orientation_confirmed": confirmed,
                                 "yaw_confirmed": confirmed, "pitch_confirmed": confirmed, "roll_confirmed": roll_confirmed,
                                 "projection_checked": True, "projection_residual_px": None},
        "prior": {"enabled": True, "direction_type": direction_type, "solver_role": role, "quality": "unverified"},
    }


def _prior(**kwargs):
    frame = _keyframe(**kwargs)
    track = normalize_camera_track({"schema_version": 2, "coordinate_system": "web_cad_world", "pose_convention": {}, "keyframes": [frame]})
    return build_orientation_prior(track["keyframes"][0], origin_xy=(100.0, 200.0), cad_scale=0.5)


def test_frontend_pose_conversion_and_basis_reuse_existing_camera_formula():
    prior = _prior()
    state = web_camera_to_python_state(_keyframe()["camera"], origin_xy=(100.0, 200.0), cad_scale=0.5)
    expected = _camera_to_world_rotation(state)

    assert np.allclose(prior.position_cad_m, [5.0, 10.0, 15.0])
    assert np.allclose(prior.rotation_cad_from_camera, expected)
    assert np.allclose(prior.camera_forward_cad, expected[:, 2])
    assert np.allclose(prior.camera_up_cad, -expected[:, 1])
    assert np.allclose(prior.camera_right_cad, expected[:, 0])
    assert np.allclose(prior.rotation_cad_from_camera.T @ prior.rotation_cad_from_camera, np.eye(3))
    assert np.linalg.det(prior.rotation_cad_from_camera) == pytest.approx(1.0)


def test_camera_forward_does_not_require_roll_confirmation_but_up_and_right_do():
    forward = _prior(direction_type="camera_forward", roll_confirmed=False)
    up = _prior(direction_type="camera_up", roll_confirmed=False)
    right = _prior(direction_type="camera_right", roll_confirmed=False)
    direction = np.asarray([1.0, 0.0, 0.0])

    assert qualify_orientation_prior(forward, direction).accepted
    assert "roll-not-confirmed" in qualify_orientation_prior(up, direction).rejection_reasons
    assert "roll-not-confirmed" in qualify_orientation_prior(right, direction).rejection_reasons


def test_prior_requires_confirmed_manual_orientation_and_nonparallel_direction():
    prior = _prior()
    near_parallel = qualify_orientation_prior(prior, prior.direction_vector_cad)
    unconfirmed = qualify_orientation_prior(_prior(confirmed=False), np.asarray([1.0, 0.0, 0.0]))
    algorithm = qualify_orientation_prior(_prior(source="algorithm_prediction"), np.asarray([1.0, 0.0, 0.0]))

    assert near_parallel.accepted is False
    assert "prior-near-parallel-to-trajectory" in near_parallel.rejection_reasons
    assert unconfirmed.accepted is False
    assert "orientation-not-confirmed" in unconfirmed.rejection_reasons
    assert algorithm.accepted is False
    assert "orientation-source-not-allowed" in algorithm.rejection_reasons


def test_nonparallel_direction_is_accepted_with_default_twenty_degree_gate():
    prior = _prior(yaw=90.0, pitch=0.0)
    outcome = qualify_orientation_prior(prior, np.asarray([0.0, 1.0, 0.0]), PriorQualificationConfig())

    assert outcome.accepted
    assert outcome.angle_to_axis_deg == 90.0


def test_invalid_direction_and_metadata_only_types_are_rejected():
    cad_up = _prior(direction_type="cad_up")
    invalid = replace(_prior(), direction_vector_cad=np.asarray([float("nan"), 0.0, 0.0]))

    assert "cad-up-metadata-only" in qualify_orientation_prior(cad_up, np.asarray([1.0, 0.0, 0.0])).rejection_reasons
    assert "direction-not-finite" in qualify_orientation_prior(invalid, np.asarray([1.0, 0.0, 0.0])).rejection_reasons


def test_solve_validate_split_requires_distinct_frames_and_holdout():
    solve = _prior(frame=1, role="solve", yaw=90.0, pitch=0.0)
    validate = _prior(frame=2, role="validate", yaw=90.0, pitch=0.0)
    duplicate = _prior(frame=1, role="validate", yaw=90.0, pitch=0.0)

    assert validate_prior_split([solve]).accepted is False
    assert "missing-validate-anchor" in validate_prior_split([solve]).rejection_reasons
    assert validate_prior_split([solve, validate]).accepted
    report = validate_prior_split([solve, duplicate])
    assert report.accepted is False
    assert report.duplicate_frame_indices == (1,)
