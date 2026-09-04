from __future__ import annotations

import pytest

from cadscene.sfm.resolution import (
    normalize_reconstruction_resolution,
    reconstruction_dimensions,
    target_height_for_resolution,
)


def test_missing_resolution_defaults_to_1080p() -> None:
    assert normalize_reconstruction_resolution(None) == "1080p"
    assert target_height_for_resolution(None) == 1080


@pytest.mark.parametrize(
    ("value", "expected"),
    [("720p", 720), ("1080p", 1080), ("source", None)],
)
def test_supported_resolutions_map_to_target_heights(value, expected) -> None:
    assert normalize_reconstruction_resolution(value) == value
    assert target_height_for_resolution(value) == expected


@pytest.mark.parametrize("value", ["", "4k", "2160p", 1080, True])
def test_invalid_resolution_is_rejected(value) -> None:
    with pytest.raises(ValueError, match="reconstruction_resolution"):
        normalize_reconstruction_resolution(value)


def test_4k_source_scales_to_requested_even_dimensions() -> None:
    assert reconstruction_dimensions(3840, 2160, "1080p") == (1920, 1080)
    assert reconstruction_dimensions(3840, 2160, "720p") == (1280, 720)
    assert reconstruction_dimensions(4096, 2160, "1080p") == (2048, 1080)


def test_small_source_is_not_upscaled_and_source_is_unchanged() -> None:
    assert reconstruction_dimensions(1280, 720, "1080p") == (1280, 720)
    assert reconstruction_dimensions(3840, 2160, "source") == (3840, 2160)


def test_scaled_width_is_rounded_down_to_an_even_integer() -> None:
    width, height = reconstruction_dimensions(4001, 2160, "720p")
    assert (width, height) == (1332, 720)
    assert width % 2 == 0 and height % 2 == 0


@pytest.mark.parametrize(("width", "height"), [(0, 1080), (1920, 0), (-1, 720)])
def test_invalid_source_dimensions_are_rejected(width, height) -> None:
    with pytest.raises(ValueError, match="source video dimensions"):
        reconstruction_dimensions(width, height, "1080p")
