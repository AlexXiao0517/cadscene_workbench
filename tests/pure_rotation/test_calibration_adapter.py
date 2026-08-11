from __future__ import annotations

from cadscene.cli.prepare_pure_rotation_calibration import scale_camera_parameters


def test_scale_camera_parameters_preserves_normalized_intrinsics() -> None:
    scaled = scale_camera_parameters(
        model="OPENCV",
        source_width=2688,
        source_height=1512,
        target_width=1920,
        target_height=1080,
        parameters=(
            1721.369663340093,
            1889.1135310868447,
            1344.0,
            756.0,
            -0.092,
            0.085,
            0.001,
            0.0007,
        ),
    )

    assert scaled[:4] == (
        1721.369663340093 * 1920 / 2688,
        1889.1135310868447 * 1080 / 1512,
        960.0,
        540.0,
    )
    assert scaled[4:] == (-0.092, 0.085, 0.001, 0.0007)
