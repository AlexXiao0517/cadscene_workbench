from __future__ import annotations


SUPPORTED_RECONSTRUCTION_RESOLUTIONS = ("720p", "1080p", "source")

_TARGET_HEIGHTS: dict[str, int | None] = {
    "720p": 720,
    "1080p": 1080,
    "source": None,
}


def normalize_reconstruction_resolution(value: object = None) -> str:
    normalized = "1080p" if value is None else value
    if not isinstance(normalized, str) or normalized not in _TARGET_HEIGHTS:
        allowed = ", ".join(SUPPORTED_RECONSTRUCTION_RESOLUTIONS)
        raise ValueError(
            f"reconstruction_resolution must be one of: {allowed}"
        )
    return normalized


def target_height_for_resolution(value: object = None) -> int | None:
    return _TARGET_HEIGHTS[normalize_reconstruction_resolution(value)]


def reconstruction_dimensions(
    width: int,
    height: int,
    value: object = None,
) -> tuple[int, int]:
    source_width = int(width)
    source_height = int(height)
    if source_width <= 0 or source_height <= 0:
        raise ValueError("source video dimensions must be positive")
    target_height = target_height_for_resolution(value)
    if target_height is None or source_height <= target_height:
        return source_width, source_height
    scaled_width = int(source_width * (target_height / source_height))
    scaled_width -= scaled_width % 2
    scaled_height = target_height - target_height % 2
    return max(2, scaled_width), max(2, scaled_height)
