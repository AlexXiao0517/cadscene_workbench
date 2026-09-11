from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

import numpy as np


@dataclass(frozen=True)
class CadTextAnnotations:
    points_xy: np.ndarray
    labels: tuple[str, ...]
    heights_m: np.ndarray
    rotations_deg: np.ndarray


_UNICODE_ESCAPE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")


def clean_dxf_text(value: str) -> str:
    text = _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), str(value))
    text = text.replace("\\P", " ").replace("%%d", "°").replace("%%p", "±")
    text = re.sub(r"\\[A-Za-z][^;{}]*;", "", text)
    return text.replace("{", "").replace("}", "").strip()


def load_dxf_text_annotations(
    path: str | Path,
    *,
    origin_xy: tuple[float, float],
    cad_scale: float,
) -> CadTextAnnotations:
    annotations: list[tuple[float, float, str, float, float]] = []
    current_type = ""
    fields: dict[int, list[str]] = {}

    def flush() -> None:
        if current_type not in {"TEXT", "MTEXT", "ATTRIB"}:
            return
        label = clean_dxf_text("".join(fields.get(3, []) + fields.get(1, [])))
        aligned = bool(fields.get(11)) and (
            fields.get(72, ["0"])[0].strip() != "0"
            or fields.get(73, ["0"])[0].strip() != "0"
        )
        xs = fields.get(11) if aligned else fields.get(10)
        ys = fields.get(21) if aligned else fields.get(20)
        if not label or not xs or not ys:
            return
        try:
            x = (float(xs[0]) - origin_xy[0]) * cad_scale
            y = (float(ys[0]) - origin_xy[1]) * cad_scale
            height = max(1e-6, float(fields.get(40, ["1"])[0]) * cad_scale)
            rotation = float(fields.get(50, ["0"])[0])
        except (TypeError, ValueError):
            return
        annotations.append((x, y, label, height, rotation))

    with Path(path).open("r", encoding="utf-8", errors="replace", newline="") as stream:
        while True:
            code_line = stream.readline()
            value_line = stream.readline()
            if not code_line or not value_line:
                break
            try:
                code = int(code_line.strip())
            except ValueError:
                continue
            value = value_line.rstrip("\r\n")
            if code == 0:
                flush()
                current_type = value.strip().upper()
                fields = {}
            elif current_type:
                fields.setdefault(code, []).append(value)
        flush()
    unique = []
    seen = set()
    for item in annotations:
        key = (round(item[0] * 10), round(item[1] * 10), item[2])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return CadTextAnnotations(
        points_xy=np.asarray([[item[0], item[1]] for item in unique], dtype=np.float64).reshape(-1, 2),
        labels=tuple(item[2] for item in unique),
        heights_m=np.asarray([item[3] for item in unique], dtype=np.float64),
        rotations_deg=np.asarray([item[4] for item in unique], dtype=np.float64),
    )


def load_design_text_annotations(
    path: str | Path,
    *,
    origin_xy: tuple[float, float],
    cad_scale: float,
) -> CadTextAnnotations:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    coordinate_mode = str((payload.get("meta") or {}).get("coordinate_mode", "cad_world"))
    annotations: list[tuple[float, float, str, float, float]] = []
    for layer in payload.get("layers", ()):
        if not isinstance(layer, dict):
            continue
        for entity in layer.get("entities", ()):
            if not isinstance(entity, dict) or str(entity.get("entity_type", "")).upper() not in {
                "TEXT", "MTEXT", "ATTRIB"
            }:
                continue
            position = entity.get("world_position")
            if not isinstance(position, list) or len(position) < 2:
                points = entity.get("world_points")
                position = points[0] if isinstance(points, list) and points else None
            label = clean_dxf_text(str(entity.get("text") or ""))
            if not isinstance(position, list) or len(position) < 2 or not label:
                continue
            try:
                x, y = float(position[0]), float(position[1])
                height = float(entity.get("cad_height", 1.0))
                rotation = float(entity.get("cad_rotation", 0.0))
            except (TypeError, ValueError):
                continue
            if coordinate_mode == "cad_world":
                x = (x - origin_xy[0]) * cad_scale
                y = (y - origin_xy[1]) * cad_scale
                height *= cad_scale
            annotations.append((x, y, label, max(1e-6, height), rotation))
    return CadTextAnnotations(
        points_xy=np.asarray([[item[0], item[1]] for item in annotations], dtype=np.float64).reshape(-1, 2),
        labels=tuple(item[2] for item in annotations),
        heights_m=np.asarray([item[3] for item in annotations], dtype=np.float64),
        rotations_deg=np.asarray([item[4] for item in annotations], dtype=np.float64),
    )
