from __future__ import annotations

from pathlib import Path

import numpy as np

from cadscene.terrain.context import build_terrain_context, write_terrain_context
from cadscene.terrain.tpkg import TerrainControlSet, TerrainSourceSummary


def _controls() -> TerrainControlSet:
    summary = TerrainSourceSummary(
        source_id="a" * 64,
        path="terrain.tpkg",
        feature_count=3,
        vertex_count=3,
        geometry_types={"PointZ": 3},
        layers={"tx_feature_1": 3},
        bbox_lon_lat=(0.0, 0.0, 2.0, 0.0),
        z_range_m=(120.0, 122.0),
    )
    return TerrainControlSet(
        points_xyz=np.asarray([[0, 0, 120], [10, 0, 121], [20, 0, 122]], dtype=float),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
        point_colors_bgr=np.asarray([[1, 2, 3]] * 3, dtype=np.uint8),
        segment_colors_bgr=np.empty((0, 3), dtype=np.uint8),
        segment_widths=np.empty(0, dtype=np.int64),
        point_labels=("一", "二", "三"),
        point_source_ids=(summary.source_id,) * 3,
        segment_source_ids=(),
        sources=(summary,),
        fingerprint="b" * 64,
    )


def test_context_modes_follow_route_coverage_threshold() -> None:
    controls = _controls()
    effective = build_terrain_context(
        controls, np.asarray([[0, 0], [10, 0], [20, 0]], dtype=float),
        cad_fingerprint="cad", georeference_fingerprint="geo",
    )
    partial = build_terrain_context(
        controls, np.asarray([[0, 0], [10, 0], [500, 0]], dtype=float),
        cad_fingerprint="cad", georeference_fingerprint="geo",
    )
    relative = build_terrain_context(
        None, np.asarray([[0, 0]], dtype=float),
        cad_fingerprint="cad", georeference_fingerprint="geo", warnings=("invalid",),
    )

    assert effective.mode == "terrain"
    assert effective.coverage_fraction == 1.0
    assert partial.mode == "partial"
    assert partial.coverage_fraction == 2 / 3
    assert relative.mode == "relative"
    assert relative.warnings == ("invalid",)


def test_context_artifacts_bind_cad_georeference_and_sources(tmp_path: Path) -> None:
    context = build_terrain_context(
        _controls(), np.asarray([[0, 0]], dtype=float),
        cad_fingerprint="cad-rev", georeference_fingerprint="geo-rev",
    )

    json_path, controls_path = write_terrain_context(tmp_path, context, _controls())

    assert json_path.is_file() and controls_path.is_file()
    assert context.cad_fingerprint == "cad-rev"
    assert context.georeference_fingerprint == "geo-rev"
    assert context.source_fingerprints == ("a" * 64,)
    arrays = np.load(controls_path)
    assert arrays["points_xyz"].shape == (3, 3)


def test_context_reports_uncovered_intervals_and_cross_source_conflicts() -> None:
    first = _controls()
    second_summary = TerrainSourceSummary(
        source_id="c" * 64,
        path="other.tpkg",
        feature_count=1,
        vertex_count=1,
        geometry_types={"PointZ": 1},
        layers={"tx_feature_1": 1},
        bbox_lon_lat=(0.0, 0.0, 0.0, 0.0),
        z_range_m=(130.0, 130.0),
    )
    controls = TerrainControlSet(
        points_xyz=np.vstack((first.points_xyz, [[0.5, 0.0, 130.0]])),
        segment_starts_xyz=np.empty((0, 3)),
        segment_ends_xyz=np.empty((0, 3)),
        point_colors_bgr=np.vstack((first.point_colors_bgr, [[1, 2, 3]])).astype(np.uint8),
        segment_colors_bgr=np.empty((0, 3), dtype=np.uint8),
        segment_widths=np.empty(0, dtype=np.int64),
        point_labels=(*first.point_labels, "冲突"),
        point_source_ids=(*first.point_source_ids, second_summary.source_id),
        segment_source_ids=(),
        sources=(*first.sources, second_summary),
        fingerprint="d" * 64,
    )

    context = build_terrain_context(
        controls,
        np.asarray([[0, 0], [500, 0], [510, 0], [10, 0]], dtype=float),
        route_time_sec=(0.0, 1.0, 2.0, 3.0),
        cad_fingerprint="cad",
        georeference_fingerprint="geo",
    )

    assert context.uncovered_intervals == (
        {"start_index": 1, "end_index": 2, "start_time_sec": 1.0, "end_time_sec": 2.0},
    )
    assert len(context.conflicts) >= 1
    assert any("高程冲突" in warning for warning in context.warnings)
