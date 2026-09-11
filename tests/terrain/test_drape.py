from __future__ import annotations

import numpy as np
import pytest

from cadscene.terrain.drape import detect_terrain_conflicts, drape_cad_segments
from cadscene.terrain.tpkg import TerrainControlSet, TerrainSourceSummary


def _source(source_id: str, z: float) -> TerrainSourceSummary:
    return TerrainSourceSummary(
        source_id=source_id,
        path=f"{source_id}.tpkg",
        feature_count=1,
        vertex_count=2,
        geometry_types={"LineStringZ": 1},
        layers={"route": 1},
        bbox_lon_lat=(0.0, 0.0, 1.0, 1.0),
        z_range_m=(z, z),
    )


def _controls() -> TerrainControlSet:
    source = _source("a" * 64, 132.5)
    return TerrainControlSet(
        points_xyz=np.asarray([[80.0, 0.0, 145.0]], dtype=np.float64),
        point_source_ids=(source.source_id,),
        point_labels=("",),
        point_colors_bgr=np.asarray([[255, 255, 255]], dtype=np.uint8),
        segment_starts_xyz=np.asarray([[0.0, 0.0, 132.5]], dtype=np.float64),
        segment_ends_xyz=np.asarray([[20.0, 0.0, 134.5]], dtype=np.float64),
        segment_source_ids=(source.source_id,),
        segment_colors_bgr=np.asarray([[0, 255, 0]], dtype=np.uint8),
        segment_widths=np.asarray([2], dtype=np.int32),
        sources=(source,),
        fingerprint="c" * 64,
    )


def test_drape_uses_nearest_control_and_preserves_cad_color() -> None:
    starts = np.asarray([[0.0, 1.0], [500.0, 500.0]], dtype=np.float64)
    ends = np.asarray([[20.0, 1.0], [510.0, 500.0]], dtype=np.float64)
    colors = np.asarray([[10, 20, 30], [40, 50, 60]], dtype=np.uint8)

    result = drape_cad_segments(
        starts,
        ends,
        colors,
        _controls(),
        sample_step_m=2.0,
        max_distance_m=160.0,
    )

    assert result.source_segment_indices.tolist() == [0]
    assert result.starts_xyz[0].tolist() == pytest.approx([0.0, 1.0, 132.5])
    assert result.ends_xyz[0].tolist() == pytest.approx([20.0, 1.0, 134.5])
    assert result.colors_bgr.tolist() == [[10, 20, 30]]
    assert result.source_ids == ("a" * 64,)


def test_conflicts_report_close_controls_with_large_height_difference() -> None:
    first = _source("a" * 64, 100.0)
    second = _source("b" * 64, 105.0)
    controls = TerrainControlSet(
        points_xyz=np.asarray([[0.0, 0.0, 100.0], [1.0, 0.0, 105.0]]),
        point_source_ids=(first.source_id, second.source_id),
        point_labels=("", ""),
        point_colors_bgr=np.asarray([[0, 0, 0], [0, 0, 0]], dtype=np.uint8),
        segment_starts_xyz=np.empty((0, 3), dtype=np.float64),
        segment_ends_xyz=np.empty((0, 3), dtype=np.float64),
        segment_source_ids=(),
        segment_colors_bgr=np.empty((0, 3), dtype=np.uint8),
        segment_widths=np.empty((0,), dtype=np.int32),
        sources=(first, second),
        fingerprint="c" * 64,
    )

    conflicts = detect_terrain_conflicts(
        controls,
        horizontal_threshold_m=2.0,
        vertical_threshold_m=3.0,
    )

    assert len(conflicts) == 1
    assert conflicts[0].source_ids == (first.source_id, second.source_id)
    assert conflicts[0].horizontal_distance_m == pytest.approx(1.0)
    assert conflicts[0].vertical_difference_m == pytest.approx(5.0)
