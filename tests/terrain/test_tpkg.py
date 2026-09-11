from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import struct

import numpy as np
import pytest

from cadscene.terrain.tpkg import (
    TerrainValidationError,
    inspect_tpkg,
    load_tpkg,
    merge_terrain_controls,
    route_coverage,
)


def _gpkg_geometry(
    base_type: int,
    points: list[tuple[float, float, float]],
    *,
    with_z: bool = True,
) -> bytes:
    geometry_type = base_type | (0x80000000 if with_z else 0)
    header = b"GP\x00\x01" + struct.pack("<i", 4326)
    wkb = bytearray(struct.pack("<BI", 1, geometry_type))
    if base_type == 1:
        values = points[0]
        wkb.extend(struct.pack("<ddd" if with_z else "<dd", *values[: 3 if with_z else 2]))
    elif base_type == 2:
        wkb.extend(struct.pack("<I", len(points)))
        for values in points:
            wkb.extend(struct.pack("<ddd" if with_z else "<dd", *values[: 3 if with_z else 2]))
    else:
        raise ValueError(base_type)
    return header + bytes(wkb)


def _write_tpkg(
    path: Path,
    *,
    z: float,
    longitude_offset: float = 0.0,
    include_point: bool = True,
    with_z: bool = True,
) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("create table tx_feature_1 (fid integer primary key, layer text, geom blob)")
    connection.execute(
        "create table tx_feature_1_attribute "
        "(fid integer primary key, name text, geometry_style text, visible integer)"
    )
    line = [
        (119.0 + longitude_offset, 28.0, z),
        (119.001 + longitude_offset, 28.001, z + 2.0),
    ]
    connection.execute(
        "insert into tx_feature_1 values (?, ?, ?)",
        (1, "route", _gpkg_geometry(2, line, with_z=with_z)),
    )
    connection.execute(
        "insert into tx_feature_1_attribute values (?, ?, ?, ?)",
        (
            1,
            "路线",
            json.dumps(
                {
                    "heightReference": 1,
                    "polylineStyle": {
                        "lineSymbol": {
                            "width": 4,
                            "material": {"color": [1.0, 0.5, 0.0]},
                        }
                    },
                }
            ),
            1,
        ),
    )
    if include_point:
        connection.execute(
            "insert into tx_feature_1 values (?, ?, ?)",
            (2, "station", _gpkg_geometry(1, [(119.0005, 28.0005, z + 1.0)])),
        )
        connection.execute(
            "insert into tx_feature_1_attribute values (?, ?, ?, ?)",
            (2, "K181", "{}", 1),
        )
    connection.commit()
    connection.close()
    return path


def _project(longitude: float, latitude: float) -> tuple[float, float]:
    return (longitude - 119.0) * 100_000.0, (latitude - 28.0) * 100_000.0


def test_inspect_and_load_tpkg_preserves_xyz_style_and_labels(tmp_path: Path) -> None:
    path = _write_tpkg(tmp_path / "route.tpkg", z=125.0)

    summary = inspect_tpkg(path)
    controls = load_tpkg(path, project_lon_lat=_project)

    assert summary.source_id == hashlib.sha256(path.read_bytes()).hexdigest()
    assert summary.feature_count == 2
    assert summary.geometry_types == {"LineStringZ": 1, "PointZ": 1}
    assert summary.z_range_m == pytest.approx((125.0, 127.0))
    np.testing.assert_allclose(controls.segment_starts_xyz, [[0.0, 0.0, 125.0]])
    np.testing.assert_allclose(controls.segment_ends_xyz, [[100.0, 100.0, 127.0]])
    assert controls.segment_colors_bgr.tolist() == [[0, 128, 255]]
    assert controls.segment_widths.tolist() == [4]
    np.testing.assert_allclose(controls.points_xyz, [[50.0, 50.0, 126.0]])
    assert controls.point_labels == ("K181",)
    assert controls.segment_source_ids == (summary.source_id,)


def test_merge_is_independent_of_upload_order(tmp_path: Path) -> None:
    first = load_tpkg(_write_tpkg(tmp_path / "a.tpkg", z=120.0), project_lon_lat=_project)
    second = load_tpkg(
        _write_tpkg(tmp_path / "b.tpkg", z=130.0, longitude_offset=0.01),
        project_lon_lat=_project,
    )

    left = merge_terrain_controls((first, second))
    right = merge_terrain_controls((second, first))

    assert left.fingerprint == right.fingerprint
    assert left.segment_source_ids == right.segment_source_ids
    assert left.segment_starts_xyz.tolist() == right.segment_starts_xyz.tolist()
    assert tuple(item.source_id for item in left.sources) == tuple(
        sorted((first.sources[0].source_id, second.sources[0].source_id))
    )


def test_route_coverage_uses_nearest_point_or_sampled_segment(tmp_path: Path) -> None:
    controls = load_tpkg(
        _write_tpkg(tmp_path / "route.tpkg", z=125.0),
        project_lon_lat=_project,
    )
    route = np.asarray([[0.0, 0.0], [50.0, 50.0], [1000.0, 1000.0]])

    coverage = route_coverage(route, controls, max_distance_m=160.0)

    assert coverage.covered_count == 2
    assert coverage.total_count == 3
    assert coverage.fraction == pytest.approx(2.0 / 3.0)
    assert coverage.mode == "partial"


def test_tpkg_without_z_is_rejected(tmp_path: Path) -> None:
    path = _write_tpkg(tmp_path / "flat.tpkg", z=0.0, with_z=False)

    with pytest.raises(TerrainValidationError, match="Z"):
        inspect_tpkg(path)
