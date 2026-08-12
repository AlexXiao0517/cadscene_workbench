from __future__ import annotations

import math
import re
from typing import Any, Iterator


_STATION_PATTERN = re.compile(
    r"(?:^|\b)(?:[A-Z]{0,3})?K?\d+\+\d+(?:\.\d+)?(?:$|\b)",
    re.IGNORECASE,
)
_STATION_LAYER_TOKENS = ("桩号", "station", "chainage")


def iter_text_entities(modelspace: Any) -> Iterator[tuple[Any, Any | None]]:
    """Yield supported modelspace text and visible attributes with their INSERT."""

    for entity in modelspace:
        entity_type = entity.dxftype()
        if entity_type in {"TEXT", "MTEXT"}:
            yield entity, None
        elif entity_type == "INSERT":
            for attrib in entity.attribs:
                if not attrib.is_invisible:
                    yield attrib, entity


def _xy_rotation(direction: Any) -> float:
    if math.isclose(float(direction.x), 0.0) and math.isclose(float(direction.y), 0.0):
        return 0.0
    return math.degrees(math.atan2(float(direction.y), float(direction.x))) % 360.0


def _text_alignment(name: str) -> tuple[str, str]:
    normalized = name.upper()
    if normalized.endswith("RIGHT"):
        horizontal = "right"
    elif normalized in {"CENTER", "MIDDLE"} or normalized.endswith("CENTER"):
        horizontal = "center"
    else:
        horizontal = "left"

    if normalized.startswith("TOP"):
        vertical = "top"
    elif normalized == "MIDDLE" or normalized.startswith("MIDDLE"):
        vertical = "middle"
    elif normalized.startswith("BOTTOM"):
        vertical = "bottom"
    else:
        vertical = "baseline"
    return horizontal, vertical


def _mtext_alignment(value: int) -> tuple[str, str]:
    row, column = divmod(max(1, min(9, value)) - 1, 3)
    return ("left", "center", "right")[column], ("top", "middle", "bottom")[row]


def _effective_layer(entity: Any, parent_insert: Any | None) -> str:
    layer = str(entity.dxf.layer)
    if parent_insert is not None and layer == "0":
        return str(parent_insert.dxf.layer)
    return layer


def classify_text_role(text: str, layer: str) -> str:
    is_station = bool(_STATION_PATTERN.search(text)) or any(
        token in layer.lower() for token in _STATION_LAYER_TOKENS
    )
    return "station" if is_station else "annotation"


def extract_text_entity(
    entity: Any,
    parent_insert: Any | None = None,
) -> dict[str, Any] | None:
    """Normalize an ezdxf TEXT, MTEXT, or ATTRIB into viewer CAD fields."""

    entity_type = entity.dxftype()
    if entity_type in {"TEXT", "ATTRIB"}:
        text = entity.plain_text().strip()
        if not text:
            return None
        alignment, point, second = entity.get_placement()
        position = entity.ocs().to_wcs(point)
        if second is not None:
            direction = entity.ocs().to_wcs(second) - position
        else:
            angle = math.radians(float(entity.dxf.get("rotation", 0.0)))
            direction = entity.ocs().to_wcs(
                (math.cos(angle), math.sin(angle), 0.0)
            )
        horizontal, vertical = _text_alignment(alignment.name)
        height = float(entity.dxf.get("height", 1.0))
    elif entity_type == "MTEXT":
        text = entity.plain_text(split=False, fast=False).strip()
        if not text:
            return None
        position = entity.dxf.insert
        direction = entity.ucs().ux
        horizontal, vertical = _mtext_alignment(
            int(entity.dxf.get("attachment_point", 1))
        )
        height = float(entity.dxf.get("char_height", 1.0))
    else:
        return None

    layer = _effective_layer(entity, parent_insert)
    world_position = [float(position.x), float(position.y), float(position.z)]
    item: dict[str, Any] = {
        "entity_id": str(getattr(entity.dxf, "handle", "") or entity_type),
        "entity_type": entity_type,
        "type": "text",
        "text": text,
        "layer": layer,
        "world_position": world_position,
        "world_points": [world_position],
        "cad_rotation": _xy_rotation(direction),
        "cad_height": max(height, 1e-9),
        "text_lines": len(text.splitlines()) or 1,
        "horizontal_align": horizontal,
        "vertical_align": vertical,
        "text_role": classify_text_role(text, layer),
    }
    if entity_type == "ATTRIB":
        item["attribute_tag"] = str(entity.dxf.tag)
        item["block_name"] = (
            str(parent_insert.dxf.name) if parent_insert is not None else ""
        )
    return item
