from dataclasses import replace

import numpy as np
import pytest

from cadscene.terrain.context import TerrainContext
from cadscene.terrain.height_reference import select_height_reference
from cadscene.terrain.tpkg import TerrainControlSet


def _inputs():
    controls = TerrainControlSet(
        points_xyz=np.array([[0., 0., 120.], [10., 0., 125.]]),
        point_source_ids=("a", "a"), point_labels=("", ""),
        point_colors_bgr=np.zeros((2, 3), dtype=np.uint8),
        segment_starts_xyz=np.empty((0, 3)), segment_ends_xyz=np.empty((0, 3)),
        segment_source_ids=(), segment_colors_bgr=np.empty((0, 3)), segment_widths=np.empty(0),
        sources=(), fingerprint="a" * 64,
    )
    context = TerrainContext(
        mode="terrain", coverage_fraction=1, covered_route_points=2, total_route_points=2,
        max_control_distance_m=160, height_range_m=(120, 125), source_fingerprints=("a",),
        controls_fingerprint="terrain", cad_fingerprint="cad", georeference_fingerprint="geo",
    )
    return context, controls


@pytest.mark.parametrize("values", [[194., None], [float("inf"), 200.], []])
def test_incomplete_absolute_heights_disable_terrain_for_whole_route(values):
    context, controls = _inputs()
    result = select_height_reference(context, controls, values)
    assert result.mode == "relative"
    assert result.cad_fallback_ground_m == 0
    assert result.camera_height_datum_source == "relative_fallback_missing_abs_alt"


def test_partial_coverage_keeps_absolute_reference_and_ground_fallback():
    context, controls = _inputs()
    result = select_height_reference(replace(context, mode="partial", coverage_fraction=0.6), controls, [194., 200.])
    assert result.mode == "partial"
    assert result.cad_fallback_ground_m == 122.5
    assert result.reference_ground_m is None
    assert result.camera_height_datum_valid is False
    assert result.camera_height_datum_source == "srt_abs_alt_unverified"


def test_uncovered_terrain_does_not_raise_relative_cad_to_absolute_ground():
    context, controls = _inputs()
    result = select_height_reference(replace(context, mode="relative", coverage_fraction=0), controls, [194., 200.])
    assert result.mode == "relative"
    assert result.cad_fallback_ground_m == 0
