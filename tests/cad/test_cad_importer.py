from __future__ import annotations

import json
from pathlib import Path

import ezdxf

from cadscene.cad.dxf_parser import decode_legacy_dxf_text
from cadscene.cad.importer import import_dxf
from cadscene.cad.loader import detect_road_centerline, load_cad_bundle


def _write_sample_dxf(path: Path, *, units: int = 6) -> None:
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    doc.layers.add("road_center", color=1)
    doc.layers.add("road_edge", color=4)
    model = doc.modelspace()
    model.add_line((100.0, 200.0), (120.0, 200.0), dxfattribs={"layer": "road_center", "color": 1})
    model.add_lwpolyline(
        [(100.0, 190.0), (110.0, 195.0), (120.0, 190.0)],
        dxfattribs={"layer": "road_edge", "color": 4},
    )
    model.add_polyline2d(
        [(100.0, 210.0), (110.0, 215.0), (120.0, 210.0)],
        dxfattribs={"layer": "misc", "color": 3},
    )
    model.add_point((105.0, 205.0), dxfattribs={"layer": "misc"})
    doc.saveas(path)


def test_import_dxf_generates_legacy_viewer_design_and_metadata(tmp_path: Path) -> None:
    source = tmp_path / "raw.dxf"
    _write_sample_dxf(source)

    result = import_dxf(source, tmp_path / "dataset")

    design = json.loads(result.design_json.read_text(encoding="utf-8"))
    assert design["meta"]["coordinate_mode"] == "cad_world"
    assert design["meta"]["bbox"] == {
        "min_x": 100.0,
        "min_y": 190.0,
        "max_x": 120.0,
        "max_y": 215.0,
    }
    assert result.stats["origin_xy"] == [100.0, 190.0]
    assert result.stats["cad_scale"] == 1.0
    assert result.stats["unit"] == "meter"
    assert result.stats["entity_count"] == 3
    assert result.stats["point_count"] == 8
    assert result.stats["segment_count"] == 5
    assert result.stats["layer_count"] == 3
    assert result.stats["aci_colors"] == [1, 3, 4]
    assert result.stats["unsupported_entities"] == {"POINT": 1}
    assert result.stats["has_road_centerline"] is True
    assert result.stats["road_centerline_source"] == "layer"
    assert any("POINT" in warning for warning in result.warnings)

    capability = detect_road_centerline(tmp_path / "dataset")
    assert capability.has_road_centerline is True
    assert capability.source == "layer"


def test_generic_site_plan_does_not_claim_a_road_centerline(tmp_path: Path) -> None:
    source = tmp_path / "site-plan.dxf"
    doc = ezdxf.new("R2010")
    model = doc.modelspace()
    model.add_lwpolyline(
        [(0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0)],
        close=True,
        dxfattribs={"layer": "future_building", "color": 3},
    )
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")

    assert result.stats["has_road_centerline"] is False
    assert result.stats["road_centerline_source"] == "none"
    capability = detect_road_centerline(tmp_path / "dataset")
    assert capability.has_road_centerline is False
    assert capability.source == "none"


def test_explicit_road_center_asset_is_detected(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    (dataset / "road_center.json").write_text(
        json.dumps([{"points": [[0, 0], [10, 0]]}]),
        encoding="utf-8",
    )

    capability = detect_road_centerline(dataset)

    assert capability.has_road_centerline is True
    assert capability.source == "asset"
    assert capability.line_count == 1


def test_line_polyline_entities_keep_id_layer_color_closed_and_bbox(tmp_path: Path) -> None:
    source = tmp_path / "raw.dxf"
    _write_sample_dxf(source)

    result = import_dxf(source, tmp_path / "dataset")
    design = json.loads(result.design_json.read_text(encoding="utf-8"))
    entities = [entity for layer in design["layers"] for entity in layer["entities"]]

    assert {entity["entity_type"] for entity in entities} == {"LINE", "LWPOLYLINE", "POLYLINE"}
    assert all(entity["entity_id"] for entity in entities)
    assert all("layer" in entity and "aci_color" in entity for entity in entities)
    assert all("closed" in entity and len(entity["bbox"]) == 4 for entity in entities)
    assert all(entity["world_points"] for entity in entities)
    assert all(entity["points"] for entity in entities)

    bundle = load_cad_bundle(tmp_path / "dataset", origin_xy=(100.0, 190.0), cad_scale=1.0)
    assert len(bundle.centers) == 1
    assert len(bundle.edges) == 1
    assert len(bundle.refs) == 1


def test_unitless_dxf_uses_legacy_scale_with_warning_and_writes_report(tmp_path: Path) -> None:
    source = tmp_path / "unitless.dxf"
    _write_sample_dxf(source, units=0)

    result = import_dxf(source, tmp_path / "dataset")

    assert result.stats["cad_scale"] == 0.06
    assert result.stats["unit"] == "unknown"
    assert any("0.06" in warning for warning in result.warnings)
    assert result.report.exists()
    report = result.report.read_text(encoding="utf-8")
    assert "CAD 导入报告" in report
    assert "POINT" in report
    assert "只负责格式转换" in report


def test_geospatial_meter_coordinates_override_incorrect_millimeter_header(tmp_path: Path) -> None:
    source = tmp_path / "survey_wrong_units.dxf"
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("中心线", color=1)
    doc.layers.add("道路", color=4)
    model = doc.modelspace()
    model.add_line((526000.0, 3375000.0), (529000.0, 3376500.0), dxfattribs={"layer": "中心线"})
    model.add_lwpolyline(
        [(526100.0, 3374900.0), (528900.0, 3376400.0)],
        dxfattribs={"layer": "道路"},
    )
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")

    assert result.stats["declared_unit"] == "millimeter"
    assert result.stats["unit"] == "meter"
    assert result.stats["cad_scale"] == 1.0
    assert result.stats["unit_inferred_override"] is True
    assert any("测绘米制坐标" in warning for warning in result.warnings)


def test_spline_arc_and_circle_are_exported_when_present(tmp_path: Path) -> None:
    source = tmp_path / "curves.dxf"
    doc = ezdxf.new("R2010")
    model = doc.modelspace()
    model.add_arc((0, 0), 10, 0, 90)
    model.add_circle((20, 20), 5)
    model.add_spline([(0, 0), (5, 10), (10, 0)])
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")
    design = json.loads(result.design_json.read_text(encoding="utf-8"))
    entity_types = {entity["entity_type"] for layer in design["layers"] for entity in layer["entities"]}

    assert entity_types == {"ARC", "CIRCLE", "SPLINE"}
    assert result.stats["entity_count"] == 3


def test_bulge_polyline_uses_legacy_dense_arc_sampling(tmp_path: Path) -> None:
    source = tmp_path / "bulge.dxf"
    doc = ezdxf.new("R2010")
    model = doc.modelspace()
    model.add_lwpolyline(
        [(0.0, 0.0, 1.0), (1000.0, 0.0, 0.0)],
        format="xyb",
        dxfattribs={"layer": "road_center", "color": 1},
    )
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")
    design = json.loads(result.design_json.read_text(encoding="utf-8"))
    entity = design["layers"][0]["entities"][0]

    # 旧主线按 2 CAD 单位弧长采样；1000 单位直径的半圆应远多于固定 64 份。
    assert len(entity["world_points"]) >= 780
    assert entity["world_points"][0] == [0.0, 0.0]
    assert entity["world_points"][-1] == [1000.0, 0.0]
    assert result.stats["segment_count"] == len(entity["world_points"]) - 1


def test_gbk_mojibake_layer_names_are_recovered() -> None:
    assert decode_legacy_dxf_text("ÖÐÐÄÏß") == "中心线"
    assert decode_legacy_dxf_text("µØÐÎÍ¼") == "地形图"
    assert decode_legacy_dxf_text("road_center") == "road_center"


def test_road_focus_bbox_ignores_separate_local_terrain_cluster(tmp_path: Path) -> None:
    source = tmp_path / "mixed-coordinate-clusters.dxf"
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 4
    doc.layers.add("ÖÐÐÄÏß", color=1)
    doc.layers.add("µØÐÎÍ¼", color=8)
    model = doc.modelspace()
    model.add_line(
        (528000.0, 3375000.0),
        (528100.0, 3375200.0),
        dxfattribs={"layer": "ÖÐÐÄÏß", "color": 1},
    )
    model.add_line(
        (91491.0, 107200.0),
        (93604.0, 109213.0),
        dxfattribs={"layer": "µØÐÎÍ¼", "color": 8},
    )
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")
    design = json.loads(result.design_json.read_text(encoding="utf-8"))

    assert {layer["name"] for layer in design["layers"]} == {"中心线", "地形图"}
    assert design["meta"]["bbox"]["min_x"] == 91491.0
    assert design["meta"]["viewer_focus_bbox"] == {
        "min_x": 528000.0,
        "min_y": 3375000.0,
        "max_x": 528100.0,
        "max_y": 3375200.0,
    }
    assert result.stats["cad_scale"] == 1.0
    assert result.stats["origin_xy"] == [528000.0, 3375000.0]
    assert result.stats["has_road_centerline"] is True
