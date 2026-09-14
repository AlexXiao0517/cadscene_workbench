"""Project-local CAD preview using the same elevation sampler as video export."""
from __future__ import annotations

from pathlib import Path
import numpy as np

from cadscene.rendering.calibrated_overlay import _drape
from cadscene.rendering.overlay import _densify_points_for_projection


def build_terrain_cad_preview(design: dict, *, controls_path: Path,
                              origin_xy: tuple[float, float], cad_scale: float,
                              route_xy: np.ndarray) -> dict:
    origin = np.asarray(origin_xy, dtype=float)
    route = np.asarray(route_xy, dtype=float).reshape(-1, 2)
    if not np.isfinite(cad_scale) or cad_scale <= 0 or not len(route):
        raise ValueError('terrain preview requires metric scale and a nonempty route')
    raw_world = design.get('meta', {}).get('coordinate_mode', 'cad_world') == 'cad_world'
    layers = [{**layer, 'entities': []} for layer in design.get('layers', [])]
    entries, starts, ends = [], [], []
    count = 0
    for layer_index, layer in enumerate(design.get('layers', [])):
        for entity in layer.get('entities', []):
            is_text = entity.get('type') == 'text'
            raw = [entity['world_position']] if is_text and 'world_position' in entity else entity.get('world_points', [])
            points = np.asarray(raw, dtype=float)
            if points.ndim != 2 or points.shape[1] < 2 or not len(points):
                continue
            xy = (points[:, :2] - origin) * cad_scale if raw_world else points[:, :2]
            if not np.isfinite(xy).all():
                continue
            if is_text:
                first = last = xy[:1]
            else:
                xy = _densify_points_for_projection(xy, 20.)
                first, last = xy[:-1], xy[1:]
            if not len(first):
                continue
            entries.append((layer_index, entity, is_text, count, len(first)))
            starts.append(first)
            ends.append(last)
            count += len(first)
    start_xy = np.vstack(starts) if starts else np.empty((0, 2))
    end_xy = np.vstack(ends) if ends else np.empty((0, 2))
    a, b, keep = _drape(start_xy, end_xy, controls_path, 'terrain')

    def web(points):
        values = np.asarray(points, dtype=float).copy()
        values[:, :2] = values[:, :2] / cad_scale + origin
        values[:, 2] /= cad_scale
        return values.tolist()

    for layer_index, entity, is_text, offset, length in entries:
        if is_text:
            if keep[offset]:
                point = a[offset].copy()
                point[2] += 0.3
                position = web([point])[0]
                layers[layer_index]['entities'].append({**entity, 'world_position': position, 'world_points': [position]})
            continue
        chunks, current = [], []
        for index in range(offset, offset + length):
            if not keep[index]:
                if current:
                    chunks.append(current)
                    current = []
                continue
            if not current:
                current.append(a[index])
            current.append(b[index])
        if current:
            chunks.append(current)
        for part, points in enumerate(chunks):
            value = {**entity, 'world_points': web(points), 'closed': False}
            if part:
                value['source_entity_id'] = entity.get('entity_id')
                value['entity_id'] = f"{entity.get('entity_id', 'cad')}:terrain:{part}"
            layers[layer_index]['entities'].append(value)
    # Used only for the camera's ground guide, not to change the camera itself.
    ground_route = route[::max(1, len(route) // 500)]
    ground, _, ground_keep = _drape(ground_route, ground_route, controls_path, 'terrain')
    meta = {**design.get('meta', {}), 'coordinate_mode': 'cad_world',
            'cad_scale': cad_scale, 'origin_xy': list(origin_xy),
            'terrain_preview': {'version': 1, 'height_units': 'cad_world',
                                'source': 'shared_export_terrain_sampler',
                                'covered_segment_count': int(keep.sum()),
                                'ground_route': web(ground[ground_keep])}}
    return {**design, 'meta': meta, 'layers': layers}
