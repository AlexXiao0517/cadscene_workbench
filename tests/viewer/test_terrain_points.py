import json
from pathlib import Path
import subprocess


def test_both_viewer_projections_keep_terrain_vertex_and_text_height():
    source = Path('apps/web_camera_viewer/viewer_legacy.js').read_text(encoding='utf-8')
    function = source[source.index('  function getWorldPoints('):source.index('  function colorFor(')]
    script = function + '\nprocess.stdout.write(JSON.stringify([' + ','.join([
        'getWorldPoints({world_points:[[10,20,138],[11,21,139]]})',
        'getWorldPoints({world_position:[10,20,138.3]})',
        'getWorldPoints({world_points:[[10,20]]})']) + ']));'
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [[[10,20,138],[11,21,139]], [[10,20,138.3]], [[10,20,0]]]


def test_camera_focus_uses_terrain_ground_instead_of_zero():
    source = Path('apps/web_camera_viewer/viewer_legacy.js').read_text(encoding='utf-8')
    assert 'function terrainGroundZ(' in source
    function = source[source.index('  function terrainGroundZ('):source.index('  function colorFor(')]
    script = function + "\nprocess.stdout.write(JSON.stringify([terrainGroundZ({x:10,y:20}, {meta:{terrain_preview:{ground_route:[[10,20,138],[100,200,150]]}}}),terrainGroundZ({x:10,y:20}, {})]));"
    result = subprocess.run(['node','-e',script],capture_output=True,text=True,check=True)
    assert json.loads(result.stdout) == [138,0]
    assert 'const groundZ = terrainGroundZ(params, data);' in source
