from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadscene.cad.dxf_parser import parse_dxf
from cadscene.cad.loader import detect_road_centerline
from cadscene.core.io import ensure_dir, write_json, write_text


LEGACY_UNKNOWN_UNIT_SCALE = 0.06
_UNIT_SCALES: dict[int, tuple[str, float]] = {
    1: ("inch", 0.0254),
    2: ("foot", 0.3048),
    3: ("mile", 1609.344),
    4: ("millimeter", 0.001),
    5: ("centimeter", 0.01),
    6: ("meter", 1.0),
    7: ("kilometer", 1000.0),
    10: ("yard", 0.9144),
}


@dataclass(frozen=True)
class CadImportResult:
    design_json: Path
    stats_json: Path
    report: Path
    stats: dict[str, Any]
    warnings: list[str]


def _unit_scale(insunits: int) -> tuple[str, float, list[str]]:
    if insunits in _UNIT_SCALES:
        unit, scale = _UNIT_SCALES[insunits]
        return unit, scale, []
    return (
        "unknown",
        LEGACY_UNKNOWN_UNIT_SCALE,
        ["DXF 未声明可识别单位，cad_scale 沿用旧主线默认值 0.06；请在高级设置中确认。"],
    )


def _looks_like_geospatial_meters(design: dict[str, Any], bbox: list[float]) -> bool:
    min_x, min_y, max_x, max_y = (float(value) for value in bbox)
    has_projected_coordinate_range = (
        max(abs(min_x), abs(max_x)) >= 100_000
        and max(abs(min_y), abs(max_y)) >= 1_000_000
        and max(abs(min_x), abs(max_x), abs(min_y), abs(max_y)) <= 100_000_000
        and max(max_x - min_x, max_y - min_y) >= 100
    )
    survey_terms = ("中心线", "道路", "路线", "桩号", "坐标", "road", "centerline", "station")
    layer_names = [str(layer.get("name", "")).lower() for layer in design.get("layers", [])]
    has_survey_layers = any(
        str(layer.get("kind", "")).lower() == "center"
        or any(term in str(layer.get("name", "")).lower() for term in survey_terms)
        for layer in design.get("layers", [])
    )
    return has_projected_coordinate_range and has_survey_layers


def _build_report(source: Path, stats: dict[str, Any], warnings: list[str]) -> str:
    unsupported = stats.get("unsupported_entities") or {}
    unsupported_text = "、".join(f"{name}: {count}" for name, count in unsupported.items()) or "无"
    warning_lines = [f"- {warning}" for warning in warnings] or ["- 无"]
    return "\n".join(
        [
            "# CAD 导入报告",
            "",
            f"- 原始 CAD：`{source}`",
            "- 文件类型：DXF",
            "- 是否转换：否",
            "- 输出：`design.json`",
            f"- 实体数量：{stats['entity_count']}",
            f"- 采样点数量：{stats['point_count']}",
            f"- 线段数量：{stats['segment_count']}",
            f"- 图层数量：{stats['layer_count']}",
            f"- ACI 颜色：{stats['aci_colors']}",
            f"- CAD bbox：{stats['bbox']}",
            f"- origin_xy：{stats['origin_xy']}",
            f"- cad_scale：{stats['cad_scale']}",
            f"- CAD 单位：{stats['unit']}",
            f"- 不支持实体：{unsupported_text}",
            "",
            "## Warnings",
            "",
            *warning_lines,
            "",
            "本步骤只负责格式转换和 viewer assets 生成，不修改 CAD 几何。",
            "",
        ]
    )


def import_dxf(source: str | Path, dataset_dir: str | Path) -> CadImportResult:
    source_path = Path(source).resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"DXF not found: {source_path}")
    output_dir = ensure_dir(dataset_dir)
    design, parse_stats = parse_dxf(source_path)
    declared_unit, cad_scale, warnings = _unit_scale(int(parse_stats.get("insunits", 0)))
    unit = declared_unit
    unit_inferred_override = False
    unsupported = parse_stats.get("unsupported_entities") or {}
    if unsupported:
        summary = "、".join(f"{name}={count}" for name, count in unsupported.items())
        warnings.append(f"以下实体类型暂未导出：{summary}")
    bbox = list(parse_stats["bbox"])
    coordinate_bbox = list(parse_stats.get("viewer_focus_bbox") or bbox)
    if declared_unit == "millimeter" and _looks_like_geospatial_meters(design, coordinate_bbox):
        unit = "meter"
        cad_scale = 1.0
        unit_inferred_override = True
        warnings.append(
            "DXF 声明单位为毫米，但坐标范围和道路/测绘图层符合测绘米制坐标；cad_scale 已自动修正为 1.0。"
        )
    stats = {
        **parse_stats,
        "source": str(source_path),
        "design_json": str((output_dir / "design.json").resolve()),
        "unit": unit,
        "declared_unit": declared_unit,
        "unit_inferred_override": unit_inferred_override,
        "cad_scale": cad_scale,
        "origin_xy": [float(coordinate_bbox[0]), float(coordinate_bbox[1])],
        "warnings": warnings,
    }
    design["meta"].update(
        {
            "unit": unit,
            "declared_unit": declared_unit,
            "unit_inferred_override": unit_inferred_override,
            "cad_scale": cad_scale,
            "origin_xy": stats["origin_xy"],
        }
    )
    design_json = write_json(output_dir / "design.json", design)
    centerline = detect_road_centerline(output_dir)
    stats["has_road_centerline"] = centerline.has_road_centerline
    stats["road_centerline_source"] = centerline.source
    stats["road_centerline_count"] = centerline.line_count
    design["meta"].update(
        {
            "has_road_centerline": centerline.has_road_centerline,
            "road_centerline_source": centerline.source,
        }
    )
    design_json = write_json(output_dir / "design.json", design)
    stats_json = write_json(output_dir / "cad_import_stats.json", stats)
    report = write_text(output_dir / "cad_import_report.md", _build_report(source_path, stats, warnings))
    return CadImportResult(
        design_json=design_json,
        stats_json=stats_json,
        report=report,
        stats=stats,
        warnings=warnings,
    )
