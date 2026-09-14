import copy
import importlib.util
import numpy as np
import pytest
from cadscene.rendering.calibrated_overlay import _drape


def test_preview_drapes_cad_in_web_units_without_changing_source(tmp_path):
    assert importlib.util.find_spec('cadscene.terrain.viewer_cad') is not None
    from cadscene.terrain.viewer_cad import build_terrain_cad_preview
    controls = tmp_path / 'controls.npz'
    np.savez(controls, points_xyz=np.empty((0, 3)),
             segment_starts_xyz=[[0., 0., 138.]], segment_ends_xyz=[[20., 0., 140.]])
    original = {'meta': {'coordinate_mode': 'cad_world'}, 'layers': [{'name': 'road', 'color': '#ff0000', 'entities': [
        {'entity_id': 'line', 'type': 'polyline', 'world_points': [[1000, 2000], [1040, 2000]]},
        {'entity_id': 'label', 'type': 'text', 'text': 'road', 'world_position': [1000, 2000]},
        {'entity_id': 'outside', 'type': 'line', 'world_points': [[3000, 2000], [3040, 2000]]}]}]}
    before = copy.deepcopy(original)
    preview = build_terrain_cad_preview(original, controls_path=controls, origin_xy=(1000, 2000),
                                         cad_scale=0.5, route_xy=np.array([[0., 0.], [20., 0.]]))
    assert original == before
    entities = preview['layers'][0]['entities']
    assert [e['entity_id'] for e in entities] == ['line', 'label']
    points = np.array(entities[0]['world_points'])
    assert points[:, 2].tolist() == pytest.approx([276., 280.])
    assert entities[1]['world_position'][2] == pytest.approx(276.6)
    a, b, keep = _drape(np.array([[0., 0.]]), np.array([[20., 0.]]), controls, 'terrain')
    assert keep[0]
    np.testing.assert_allclose(points[:, 2] * 0.5, [a[0, 2], b[0, 2]])
    assert preview['meta']['terrain_preview']['height_units'] == 'cad_world'


def test_preview_does_not_bridge_uncovered_segments(tmp_path):
    assert importlib.util.find_spec('cadscene.terrain.viewer_cad') is not None
    from cadscene.terrain.viewer_cad import build_terrain_cad_preview
    controls = tmp_path / 'controls.npz'
    np.savez(controls, points_xyz=[[0., 0., 138.]],
             segment_starts_xyz=np.empty((0,3)), segment_ends_xyz=np.empty((0,3)))
    design = {'meta': {}, 'layers': [{'entities': [
        {'entity_id': 'road', 'type': 'polyline', 'world_points': [[0,0],[200,0],[0,20]]}]}]}
    preview = build_terrain_cad_preview(design, controls_path=controls, origin_xy=(0,0), cad_scale=1,
                                        route_xy=np.array([[0., 0.]]))
    entities = preview['layers'][0]['entities']
    assert len(entities) == 2
    assert all(len(e['world_points']) >= 2 for e in entities)
    assert all(np.linalg.norm(p[:2]) <= 160 for e in entities for p in e['world_points'])


def test_preview_retains_distant_geometry_with_valid_terrain(tmp_path):
    from cadscene.terrain.viewer_cad import build_terrain_cad_preview
    controls = tmp_path / 'controls.npz'
    np.savez(controls, points_xyz=np.empty((0, 3)),
             segment_starts_xyz=[[1000., 0., 138.]], segment_ends_xyz=[[1020., 0., 138.]])
    design = {'meta': {}, 'layers': [{'entities': [
        {'entity_id': 'distant', 'type': 'polyline', 'world_points': [[1000,0],[1020,0]]}]}]}
    preview = build_terrain_cad_preview(design, controls_path=controls, origin_xy=(0,0), cad_scale=1,
                                        route_xy=np.array([[0., 0.]]))
    assert len(preview['layers'][0]['entities']) == 1
