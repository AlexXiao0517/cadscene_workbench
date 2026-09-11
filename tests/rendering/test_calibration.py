from __future__ import annotations

import numpy as np
import pytest

from cadscene.rendering.calibration import CalibratedCameraModel, output_dimensions


def test_calibration_scales_focal_and_principal_point_not_radial_terms() -> None:
    source = CalibratedCameraModel(3840, 2160, 3000.0, 1900.0, 1050.0, -0.01, 0.04)

    scaled = source.scaled_to(1920, 1080)

    assert scaled.focal_px == 1500.0
    assert scaled.cx_px == 950.0
    assert scaled.cy_px == 525.0
    assert scaled.k1 == -0.01 and scaled.k2 == 0.04


def test_user_horizontal_fov_updates_focal_but_preserves_principal_point_and_distortion() -> None:
    source = CalibratedCameraModel(1920, 1080, 1500.0, 950.0, 525.0, -0.01, 0.04)

    adjusted = source.with_horizontal_fov(90.0)

    assert adjusted.focal_px == pytest.approx(960.0)
    assert adjusted.cx_px == 950.0
    assert adjusted.cy_px == 525.0
    assert adjusted.k1 == -0.01 and adjusted.k2 == 0.04


@pytest.mark.parametrize(
    "preset,expected",
    [("720p", (1280, 720)), ("1080p", (1920, 1080)), ("source", (3840, 2160)), ("4k", (3840, 2160))],
)
def test_output_resolution_preserves_aspect_ratio(preset: str, expected: tuple[int, int]) -> None:
    assert output_dimensions(3840, 2160, preset) == expected


def test_4k_is_rejected_for_non_4k_source() -> None:
    with pytest.raises(ValueError, match="4K source"):
        output_dimensions(1920, 1080, "4k")
