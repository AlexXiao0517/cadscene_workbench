from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from cadscene.core.io import read_json


@dataclass(frozen=True)
class RoadLine:
    points: np.ndarray
    kind: str = "unknown"
    layer: str = ""
    color_bgr: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class CadBundle:
    centers: list[RoadLine]
    edges: list[RoadLine]
    refs: list[RoadLine]
    origin_xy: tuple[float, float]
    bbox_xy: tuple[float, float, float, float]


@dataclass(frozen=True)
class RoadCenterlineCapability:
    has_road_centerline: bool
    source: str
    line_count: int


_CENTERLINE_TOKENS = ("center", "centre", "middle", "zhong", "中心线", "中心", "中线")


_ACI_BGR = {
    1: (0, 0, 255),
    2: (0, 255, 255),
    3: (0, 255, 0),
    4: (255, 255, 0),
    5: (255, 0, 0),
    6: (255, 0, 255),
    7: (0, 0, 0),
    8: (128, 128, 128),
    9: (191, 191, 191),
}


def _legacy_line_color(item: dict, kind: str) -> tuple[int, int, int]:
    # 旧版 overlay 约定：edge 固定黄色，center/ref 使用各实体 ACI 颜色。
    if kind == "edge":
        return _ACI_BGR[2]
    try:
        aci = int(item.get("color", 7) or 7)
    except (TypeError, ValueError):
        aci = 7
    if aci in (0, 256):
        aci = 7
    return _ACI_BGR.get(aci, (51, 51, 51))


def _line_from_item(
    item: dict,
    kind: str,
    *,
    origin_xy: Sequence[float] | None = None,
    cad_scale: float | None = None,
) -> RoadLine:
    points = np.asarray(item.get("points", item), dtype=np.float64)
    if points.ndim != 2 or points.shape[1] < 2:
        raise ValueError("CAD line points 必须是 Nx2 或 Nx3。")
    xy = points[:, :2]
    if origin_xy is not None and cad_scale is not None:
        xy = (xy - np.asarray(origin_xy, dtype=np.float64)) * float(cad_scale)
    return RoadLine(
        points=xy,
        kind=kind,
        layer=str(item.get("layer", "")),
        color_bgr=_legacy_line_color(item, kind),
    )


def _load_lines(
    path: Path,
    kind: str,
    *,
    origin_xy: Sequence[float] | None = None,
    cad_scale: float | None = None,
) -> list[RoadLine]:
    if not path.exists():
        return []
    data = read_json(path)
    items: Sequence = data if isinstance(data, list) else data.get(
        "entities", data.get("lines", data.get("items", []))
    )
    return [
        _line_from_item(item, kind, origin_xy=origin_xy, cad_scale=cad_scale)
        for item in items
    ]


def _bbox(lines: Sequence[RoadLine]) -> tuple[float, float, float, float]:
    if not lines:
        return (0.0, 0.0, 0.0, 0.0)
    pts = np.vstack([line.points for line in lines if len(line.points)])
    return (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))


def _design_kind(layer_name: str, explicit_kind: object = None) -> str | None:
    kind = str(explicit_kind or "").lower()
    if kind in {"center", "edge", "ref"}:
        return kind
    name = layer_name.lower()
    if any(token in name for token in _CENTERLINE_TOKENS):
        return "center"
    if "edge" in name:
        return "edge"
    if "ref" in name:
        return "ref"
    return None


def _valid_line_count(items: Sequence) -> int:
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        points = item.get("points", item.get("world_points", []))
        if isinstance(points, list) and len(points) >= 2:
            count += 1
    return count


def detect_road_centerline(cad_dir: str | Path) -> RoadCenterlineCapability:
    """检测道路中心线能力，不把普通总平面线条误判为道路中心线。"""

    root = Path(cad_dir)
    asset_path = root / "road_center.json"
    if asset_path.exists():
        payload = read_json(asset_path)
        items = payload if isinstance(payload, list) else payload.get(
            "entities", payload.get("lines", payload.get("items", []))
        )
        line_count = _valid_line_count(items)
        if line_count:
            return RoadCenterlineCapability(True, "asset", line_count)

    design_path = root / "design.json"
    if design_path.exists():
        payload = read_json(design_path)
        line_count = 0
        if isinstance(payload, dict):
            for layer in payload.get("layers", []):
                if not isinstance(layer, dict):
                    continue
                if _design_kind(str(layer.get("name", "")), layer.get("kind")) != "center":
                    continue
                line_count += _valid_line_count(layer.get("entities", []))
        if line_count:
            return RoadCenterlineCapability(True, "layer", line_count)

    return RoadCenterlineCapability(False, "none", 0)


def _hex_to_bgr(value: object) -> tuple[int, int, int] | None:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None
    return blue, green, red


def _aci_to_bgr(value: object) -> tuple[int, int, int] | None:
    try:
        aci = int(value)
    except (TypeError, ValueError):
        return None
    if aci in (0, 256):
        return None
    return _ACI_BGR.get(aci)


def _design_color_bgr(item: dict, fallback: tuple[int, int, int] | None = None) -> tuple[int, int, int] | None:
    """优先保留实体颜色，缺失时才使用图层颜色。"""
    return (
        _hex_to_bgr(item.get("color"))
        or _aci_to_bgr(item.get("aci_color", item.get("color")))
        or fallback
    )


def _load_design_lines(path: Path, origin_xy: Sequence[float], cad_scale: float) -> tuple[list[RoadLine], list[RoadLine], list[RoadLine]]:
    """读取旧 viewer 导出的 design.json，并统一到 Python CAD 米坐标。"""
    if not path.exists():
        return [], [], []
    payload = read_json(path)
    if not isinstance(payload, dict):
        return [], [], []
    coordinate_mode = str((payload.get("meta") or {}).get("coordinate_mode", "cad_world"))
    origin = np.asarray(origin_xy, dtype=np.float64)
    buckets: dict[str, list[RoadLine]] = {"center": [], "edge": [], "ref": []}
    for layer in payload.get("layers", []):
        if not isinstance(layer, dict):
            continue
        kind = _design_kind(str(layer.get("name", "")), layer.get("kind"))
        if kind is None:
            continue
        layer_color_bgr = _design_color_bgr(layer)
        for entity in layer.get("entities", []):
            if not isinstance(entity, dict):
                continue
            points = np.asarray(entity.get("world_points", []), dtype=np.float64)
            if points.ndim != 2 or points.shape[1] < 2 or len(points) < 2:
                continue
            xy = points[:, :2]
            if coordinate_mode == "cad_world":
                xy = (xy - origin) * float(cad_scale)
            buckets[kind].append(
                RoadLine(
                    points=xy,
                    kind=kind,
                    layer=str(layer.get("name", "")),
                    color_bgr=_design_color_bgr(entity, layer_color_bgr),
                )
            )
    return buckets["center"], buckets["edge"], buckets["ref"]


def load_cad_bundle(
    cad_dir: str | Path,
    origin_xy: Sequence[float] | None = None,
    cad_scale: float | None = None,
) -> CadBundle:
    """读取 dataset config 指向的 CAD assets；v0.1 不负责 DXF/DWG 导出。"""

    root = Path(cad_dir)
    centers = _load_lines(
        root / "road_center.json", "center", origin_xy=origin_xy, cad_scale=cad_scale
    )
    edges = _load_lines(
        root / "road_edge.json", "edge", origin_xy=origin_xy, cad_scale=cad_scale
    )
    refs = _load_lines(
        root / "road_ref.json", "ref", origin_xy=origin_xy, cad_scale=cad_scale
    )
    if not any((centers, edges, refs)) and origin_xy is not None and cad_scale is not None:
        centers, edges, refs = _load_design_lines(root / "design.json", origin_xy, cad_scale)
    all_lines = [*centers, *edges, *refs]
    if origin_xy is None:
        origin_xy = (0.0, 0.0)
    return CadBundle(
        centers=centers,
        edges=edges,
        refs=refs,
        origin_xy=(float(origin_xy[0]), float(origin_xy[1])),
        bbox_xy=_bbox(all_lines),
    )
