from __future__ import annotations

import numpy as np

from cadscene.core.camera import CameraState
from cadscene.rendering.calibrated_overlay import project_world_points, render_calibrated_frame
from cadscene.rendering.calibration import CalibratedCameraModel


def test_radial_projection_uses_true_xyz_and_distortion() -> None:
    model = CalibratedCameraModel(100, 80, 50.0, 50.0, 40.0, 0.1, 0.0)
    uv, depth = project_world_points(
        np.asarray([[1.0, 0.0, 5.0], [0.0, 0.0, 10.0]]),
        center=np.zeros(3),
        world_from_camera=np.eye(3),
        camera=model,
    )

    assert depth.tolist() == [5.0, 10.0]
    assert uv[0, 0] > 60.0
    assert uv[1].tolist() == [50.0, 40.0]


def test_frame_renderer_preserves_original_cad_colour() -> None:
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    model = CalibratedCameraModel(100, 80, 50.0, 50.0, 40.0, 0.0, 0.0)
    camera = CameraState(0, 0, 0, 0, 0, 0, 60)
    result = render_calibrated_frame(
        frame,
        camera,
        np.asarray([[-1.0, 5.0, 0.0]]),
        np.asarray([[1.0, 5.0, 0.0]]),
        np.asarray([[12, 34, 220]], dtype=np.uint8),
        np.asarray([2]),
        model,
        overlay_alpha=1.0,
        max_distance_m=20.0,
        fade_start_m=15.0,
    )

    assert np.any(np.all(result == [12, 34, 220], axis=2))
