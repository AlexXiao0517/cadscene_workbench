from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cadscene.cad.text_entities import (
    classify_text_role,
    extract_text_entity,
    iter_text_entities,
)


SUPPORTED_ENTITY_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ARC", "CIRCLE"}
_POINT_MERGE_TOL = 1e-3
_CENTERLINE_TOKENS = ("center", "centre", "middle", "zhong", "中心线", "中心", "中线")
_EDGE_TOKENS = ("edge", "boundary", "border", "curb", "边线", "路基", "护岸")
_ROAD_FOCUS_TOKENS = (
    "中心线", "道路", "路面", "标线", "路基", "人行道", "航道", "护岸",
    "road", "centerline", "alignment",
)


@dataclass(frozen=True)
class PolylineSampleConfig:
    """与旧主线一致的 bulge 圆弧展开参数。"""

    arc_step: float = 2.0
    max_angle_step_deg: float = 2.0
    max_points: int = 20000
    prefer_virtual_entities: bool = True


DEFAULT_POLYLINE_SAMPLE = PolylineSampleConfig()


def decode_legacy_dxf_text(value: object) -> str:
    """修复部分 ANSI_936 DXF 被按西文字符返回的图层名称。"""
    text = str(value or "")
    if not any(0x80 <= ord(char) <= 0xFF for char in text):
        return text
    original_cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    for source_encoding in ("latin1", "cp1252"):
        try:
            candidate = text.encode(source_encoding).decode("gbk")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        candidate_cjk = sum("\u3400" <= char <= "\u9fff" for char in candidate)
        if candidate_cjk > original_cjk:
            return candidate
    return text


def _load_ezdxf():
    try:
        import ezdxf
        from ezdxf import colors
    except ImportError as exc:
        raise RuntimeError(
            "ezdxf not installed. Install the CAD extra before importing DXF: "
            "pip install -e .[cad]"
        ) from exc
    return ezdxf, colors


def _point2(value: Any) -> list[float]:
    return [float(value[0]), float(value[1])]


def _arc_sample_count(radius: float, span_rad: float, config: PolylineSampleConfig) -> int:
    if radius <= 0.0 or span_rad <= 0.0:
        return 2
    by_length = max(2, int(math.ceil(abs(radius * span_rad) / max(config.arc_step, 1e-9))))
    by_angle = max(
        2,
        int(math.ceil(abs(math.degrees(span_rad)) / max(config.max_angle_step_deg, 1e-9))),
    )
    return max(by_length, by_angle)


def _sample_arc(
    center: Any,
    radius: float,
    start_deg: float,
    end_deg: float,
    config: PolylineSampleConfig = DEFAULT_POLYLINE_SAMPLE,
) -> list[list[float]]:
    sweep = (float(end_deg) - float(start_deg)) % 360.0
    if math.isclose(sweep, 0.0):
        sweep = 360.0
    steps = _arc_sample_count(float(radius), math.radians(sweep), config)
    return [
        [
            float(center[0]) + radius * math.cos(math.radians(float(start_deg) + sweep * index / steps)),
            float(center[1]) + radius * math.sin(math.radians(float(start_deg) + sweep * index / steps)),
        ]
        for index in range(steps + 1)
    ]


def _sample_circle(
    center: Any,
    radius: float,
    config: PolylineSampleConfig = DEFAULT_POLYLINE_SAMPLE,
) -> list[list[float]]:
    steps = max(8, _arc_sample_count(float(radius), math.tau, config))
    return [
        [
            float(center[0]) + radius * math.cos(math.tau * index / steps),
            float(center[1]) + radius * math.sin(math.tau * index / steps),
        ]
        for index in range(steps + 1)
    ]


def _sample_bulge(
    start: list[float],
    end: list[float],
    bulge: float,
    config: PolylineSampleConfig,
) -> list[list[float]]:
    if math.isclose(float(bulge), 0.0):
        return [start, end]
    x1, y1 = start
    x2, y2 = end
    chord = math.hypot(x2 - x1, y2 - y1)
    if math.isclose(chord, 0.0):
        return [start]
    theta = 4.0 * math.atan(float(bulge))
    half_theta = abs(theta) / 2.0
    sin_half = math.sin(half_theta)
    if math.isclose(sin_half, 0.0):
        return [start, end]
    radius = chord / (2.0 * sin_half)
    ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
    px, py = -uy, ux
    midpoint = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    center_offset = math.sqrt(max(0.0, radius * radius - (chord / 2.0) ** 2))
    side = 1.0 if bulge > 0 else -1.0
    center = (midpoint[0] + side * px * center_offset, midpoint[1] + side * py * center_offset)
    start_angle = math.atan2(y1 - center[1], x1 - center[0])
    end_angle = math.atan2(y2 - center[1], x2 - center[0])
    if bulge > 0:
        while end_angle <= start_angle:
            end_angle += math.tau
    else:
        while end_angle >= start_angle:
            end_angle -= math.tau
    span = end_angle - start_angle
    steps = _arc_sample_count(radius, abs(span), config)
    points = [
        [
            center[0] + radius * math.cos(start_angle + span * index / steps),
            center[1] + radius * math.sin(start_angle + span * index / steps),
        ]
        for index in range(steps + 1)
    ]
    # 避免浮点误差让连续曲线端点出现极小裂缝。
    points[0] = list(start)
    points[-1] = list(end)
    return points


def _merge_points(target: list[list[float]], source: list[list[float]]) -> None:
    for point in source:
        if not target or math.hypot(point[0] - target[-1][0], point[1] - target[-1][1]) > _POINT_MERGE_TOL:
            target.append(point)


def _polyline_points_once(
    raw_points: list[list[float]],
    closed: bool,
    config: PolylineSampleConfig,
) -> list[list[float]]:
    if not raw_points:
        return []
    pair_count = len(raw_points) if closed else len(raw_points) - 1
    points: list[list[float]] = []
    for index in range(max(0, pair_count)):
        start = raw_points[index]
        end = raw_points[(index + 1) % len(raw_points)]
        segment = _sample_bulge(start[:2], end[:2], start[2], config)
        _merge_points(points, segment)
    return points or [raw_points[0][:2]]


def _polyline_points(raw_points: list[list[float]], closed: bool) -> list[list[float]]:
    config = DEFAULT_POLYLINE_SAMPLE
    step = config.arc_step
    points: list[list[float]] = []
    for _ in range(12):
        current = PolylineSampleConfig(
            arc_step=step,
            max_angle_step_deg=config.max_angle_step_deg,
            max_points=config.max_points,
            prefer_virtual_entities=config.prefer_virtual_entities,
        )
        points = _polyline_points_once(raw_points, closed, current)
        if len(points) <= config.max_points:
            return points
        step *= 1.5
    return points[: config.max_points]


def _entity_points(entity: Any) -> tuple[list[list[float]], bool] | None:
    entity_type = entity.dxftype()
    if entity_type == "LINE":
        return [_point2(entity.dxf.start), _point2(entity.dxf.end)], False
    if entity_type == "LWPOLYLINE":
        raw = [[float(x), float(y), float(bulge)] for x, y, bulge in entity.get_points("xyb")]
        return _polyline_points(raw, bool(entity.closed)), bool(entity.closed)
    if entity_type == "POLYLINE":
        raw = [
            [float(vertex.dxf.location.x), float(vertex.dxf.location.y), float(getattr(vertex.dxf, "bulge", 0.0))]
            for vertex in entity.vertices
        ]
        return _polyline_points(raw, bool(entity.is_closed)), bool(entity.is_closed)
    if entity_type == "ARC":
        return _sample_arc(entity.dxf.center, float(entity.dxf.radius), entity.dxf.start_angle, entity.dxf.end_angle), False
    if entity_type == "CIRCLE":
        return _sample_circle(entity.dxf.center, float(entity.dxf.radius)), True
    if entity_type == "SPLINE":
        return [[float(point.x), float(point.y)] for point in entity.flattening(0.5)], False
    return None


def _resolve_aci(entity: Any, document: Any) -> int:
    aci = int(getattr(entity.dxf, "color", 256) or 256)
    if aci in (0, 256):
        try:
            aci = abs(int(document.layers.get(str(entity.dxf.layer)).dxf.color))
        except Exception:
            aci = 7
    return aci if 1 <= aci <= 255 else 7


def _entity_color(entity: Any, document: Any, colors: Any, aci: int) -> str:
    true_color = getattr(entity.dxf, "true_color", None)
    if true_color is not None:
        rgb = ((int(true_color) >> 16) & 255, (int(true_color) >> 8) & 255, int(true_color) & 255)
    else:
        try:
            rgb = colors.aci2rgb(aci)
        except (IndexError, ValueError):
            rgb = (255, 255, 255)
    return "#{:02x}{:02x}{:02x}".format(int(rgb[0]), int(rgb[1]), int(rgb[2]))


def _bbox(points: list[list[float]]) -> list[float]:
    return [
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    ]


def _layer_kind(layer_name: str) -> str:
    name = layer_name.lower()
    if any(token in name for token in _CENTERLINE_TOKENS):
        return "center"
    if any(token in name for token in _EDGE_TOKENS):
        return "edge"
    return "ref"


def _is_road_focus_layer(layer_name: str) -> bool:
    name = layer_name.lower()
    return any(token in name for token in _ROAD_FOCUS_TOKENS)


def parse_dxf(path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ezdxf, colors = _load_ezdxf()
    source = Path(path)
    document = ezdxf.readfile(source)
    layers: dict[str, dict[str, Any]] = defaultdict(lambda: {"entities": []})
    unsupported: Counter[str] = Counter()
    text_type_counts: Counter[str] = Counter()
    aci_colors: set[int] = set()
    all_points: list[list[float]] = []
    focus_points: list[list[float]] = []

    for entity, parent_insert in iter_text_entities(document.modelspace()):
        try:
            item = extract_text_entity(entity, parent_insert)
        except Exception:
            unsupported[entity.dxftype()] += 1
            continue
        if item is None:
            continue
        layer_name = decode_legacy_dxf_text(item["layer"])
        item["text"] = decode_legacy_dxf_text(item["text"])
        item["layer"] = layer_name
        item["text_role"] = classify_text_role(item["text"], layer_name)
        raw_aci = int(getattr(entity.dxf, "color", 256) or 256)
        inherits_insert_style = parent_insert is not None and (
            raw_aci == 0
            or (raw_aci == 256 and str(getattr(entity.dxf, "layer", "0")) == "0")
        )
        style_entity = parent_insert if inherits_insert_style else entity
        aci = _resolve_aci(style_entity, document)
        color = _entity_color(style_entity, document, colors, aci)
        item.update(
            {
                "aci_color": aci,
                "color": color,
                "bbox": _bbox(item["world_points"]),
            }
        )
        layer = layers[layer_name]
        layer.setdefault("name", layer_name)
        layer.setdefault("kind", _layer_kind(layer_name))
        layer.setdefault("aci_color", aci)
        layer.setdefault("color", color)
        layer["entities"].append(item)
        text_type_counts[item["entity_type"]] += 1
        aci_colors.add(aci)
        all_points.extend(item["world_points"])
        if _is_road_focus_layer(layer_name):
            focus_points.extend(item["world_points"])

    for entity in document.modelspace():
        if entity.dxftype() in {"TEXT", "MTEXT"}:
            continue
        try:
            extracted = _entity_points(entity)
        except Exception:
            unsupported[entity.dxftype()] += 1
            continue
        if extracted is None:
            unsupported[entity.dxftype()] += 1
            continue
        points, closed = extracted
        if not points:
            unsupported[entity.dxftype()] += 1
            continue
        layer_name = decode_legacy_dxf_text(entity.dxf.layer)
        aci = _resolve_aci(entity, document)
        color = _entity_color(entity, document, colors, aci)
        entity_bbox = _bbox(points)
        item = {
            "entity_id": str(getattr(entity.dxf, "handle", "") or f"{entity.dxftype()}-{len(all_points)}"),
            "entity_type": entity.dxftype(),
            "type": "line" if entity.dxftype() == "LINE" else "polyline",
            "layer": layer_name,
            "aci_color": aci,
            "color": color,
            "closed": bool(closed),
            "bbox": entity_bbox,
            "world_points": points,
        }
        layer = layers[layer_name]
        layer.setdefault("name", layer_name)
        layer.setdefault("kind", _layer_kind(layer_name))
        layer.setdefault("aci_color", aci)
        layer.setdefault("color", color)
        layer["entities"].append(item)
        aci_colors.add(aci)
        all_points.extend(points)
        if _is_road_focus_layer(layer_name):
            focus_points.extend(points)

    if not all_points:
        raise ValueError("DXF contains no supported line entities")
    min_x, min_y, max_x, max_y = _bbox(all_points)
    focus_bbox = _bbox(focus_points) if focus_points else None
    for layer in layers.values():
        for entity in layer["entities"]:
            entity["points"] = [
                [point[0] - min_x, max_y - point[1]] for point in entity["world_points"]
            ]
    bbox = {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y}
    design = {
        "meta": {
            "source": source.name,
            "generated_by": "cadscene.cad.importer",
            "coordinate_mode": "cad_world",
            "bbox": bbox,
            "width": max_x - min_x,
            "height": max_y - min_y,
            "viewer_focus_bbox": (
                {
                    "min_x": focus_bbox[0],
                    "min_y": focus_bbox[1],
                    "max_x": focus_bbox[2],
                    "max_y": focus_bbox[3],
                }
                if focus_bbox is not None
                else None
            ),
        },
        "layers": [layers[name] for name in sorted(layers)],
    }
    parse_stats = {
        "insunits": int(document.header.get("$INSUNITS", 0) or 0),
        "entity_count": sum(len(layer["entities"]) for layer in layers.values()),
        "point_count": sum(
            len(entity["world_points"])
            for layer in layers.values()
            for entity in layer["entities"]
        ),
        "segment_count": sum(
            max(
                0,
                len(entity["world_points"])
                - 1
                + int(bool(entity.get("closed", False))),
            )
            for layer in layers.values()
            for entity in layer["entities"]
        ),
        "text_count": sum(text_type_counts.values()),
        "text_entity_types": dict(sorted(text_type_counts.items())),
        "layer_count": len(layers),
        "aci_colors": sorted(aci_colors),
        "unsupported_entities": dict(sorted(unsupported.items())),
        "bbox": [min_x, min_y, max_x, max_y],
        "viewer_focus_bbox": focus_bbox,
    }
    return design, parse_stats
