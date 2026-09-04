from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from cadscene.alignment.pose_prior_refinement import (
    ManualPose,
    PosePrior,
    fit_pose_prior_residuals,
)


def _prior(frame: int, center: tuple[float, float, float], yaw: float = 0.0) -> PosePrior:
    return PosePrior(
        frame_index=frame,
        center=np.asarray(center, dtype=np.float64),
        world_from_camera=Rotation.from_euler("z", yaw, degrees=True).as_matrix(),
    )


def _manual(
    frame: int,
    center: tuple[float, float, float],
    yaw: float = 0.0,
) -> ManualPose:
    return ManualPose(
        frame_index=frame,
        center=np.asarray(center, dtype=np.float64),
        world_from_camera=Rotation.from_euler("z", yaw, degrees=True).as_matrix(),
    )


def _yaw(rotation: np.ndarray) -> float:
    return float(Rotation.from_matrix(rotation).as_euler("zyx", degrees=True)[0])


def test_zero_keys_returns_interpolated_base_pose() -> None:
    fit = fit_pose_prior_residuals(
        [_prior(0, (0.0, 0.0, 2.0)), _prior(20, (20.0, 0.0, 2.0))],
        [],
    )

    center, rotation = fit.pose_at(10)

    np.testing.assert_allclose(center, [10.0, 0.0, 2.0])
    np.testing.assert_allclose(rotation, np.eye(3), atol=1e-10)


def test_one_key_holds_six_dof_residual_for_whole_route() -> None:
    fit = fit_pose_prior_residuals(
        [_prior(0, (0.0, 0.0, 2.0)), _prior(20, (20.0, 0.0, 2.0))],
        [_manual(10, (12.0, 3.0, 4.0), yaw=25.0)],
    )

    np.testing.assert_allclose(fit.pose_at(0)[0], [2.0, 3.0, 4.0])
    np.testing.assert_allclose(fit.pose_at(20)[0], [22.0, 3.0, 4.0])
    assert _yaw(fit.pose_at(0)[1]) == pytest.approx(25.0)
    assert _yaw(fit.pose_at(20)[1]) == pytest.approx(25.0)


def test_two_keys_interpolate_position_and_shortest_rotation_residuals() -> None:
    fit = fit_pose_prior_residuals(
        [_prior(0, (0.0, 0.0, 0.0)), _prior(10, (10.0, 0.0, 0.0))],
        [
            _manual(0, (1.0, 0.0, 0.0), yaw=170.0),
            _manual(10, (13.0, 2.0, 0.0), yaw=-170.0),
        ],
    )

    center, rotation = fit.pose_at(5)

    np.testing.assert_allclose(center, [7.0, 1.0, 0.0])
    assert abs(abs(_yaw(rotation)) - 180.0) < 1e-6
    np.testing.assert_allclose(fit.pose_at(0)[0], [1.0, 0.0, 0.0])
    np.testing.assert_allclose(fit.pose_at(10)[0], [13.0, 2.0, 0.0])


def test_manual_pose_must_reference_a_base_frame() -> None:
    with pytest.raises(ValueError, match="base pose"):
        fit_pose_prior_residuals(
            [_prior(0, (0.0, 0.0, 0.0)), _prior(10, (10.0, 0.0, 0.0))],
            [_manual(11, (11.0, 0.0, 0.0))],
        )


def test_duplicate_and_nonfinite_pose_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        fit_pose_prior_residuals(
            [_prior(0, (0.0, 0.0, 0.0)), _prior(0, (1.0, 0.0, 0.0))],
            [],
        )
    with pytest.raises(ValueError, match="finite"):
        fit_pose_prior_residuals(
            [_prior(0, (float("nan"), 0.0, 0.0))],
            [],
        )
