from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest


MODULE = Path("apps/web_camera_viewer/cad_text.js")


def _node(expression: str):
    script = (
        f"const m=require({json.dumps(str(MODULE.resolve()))}); "
        f"console.log(JSON.stringify({expression}));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_selection_rejects_offscreen_and_prefers_station_in_same_cell() -> None:
    candidates = [
        {
            "x": 10,
            "y": 10,
            "depth": 0,
            "entity": {
                "entity_id": "note",
                "text": "说明",
                "cad_height": 20,
            },
        },
        {
            "x": 12,
            "y": 12,
            "depth": 0,
            "entity": {
                "entity_id": "station",
                "text": "K1+020",
                "cad_height": 2,
            },
        },
        {
            "x": 500,
            "y": 500,
            "depth": 0,
            "entity": {"entity_id": "outside", "text": "K9+999"},
        },
    ]

    result = _node(
        f"m.selectProjectedLabels({json.dumps(candidates, ensure_ascii=False)}, "
        "{width:100,height:100,cellSize:50,maxLabels:240,margin:0})"
    )

    assert [row["entity"]["entity_id"] for row in result] == ["station"]


def test_selection_prefers_nearest_to_center_when_priority_is_equal() -> None:
    candidates = [
        {"x": 5, "y": 5, "depth": 0, "entity": {"entity_id": "far", "text": "说明"}},
        {"x": 45, "y": 45, "depth": 0, "entity": {"entity_id": "near", "text": "说明"}},
    ]

    result = _node(
        f"m.selectProjectedLabels({json.dumps(candidates, ensure_ascii=False)}, "
        "{width:100,height:100,cellSize:100,maxLabels:240,margin:0})"
    )

    assert [row["entity"]["entity_id"] for row in result] == ["near"]


def test_selection_hard_caps_labels() -> None:
    candidates = [
        {
            "x": index * 10,
            "y": 0,
            "depth": 0,
            "entity": {"entity_id": str(index), "text": f"K0+{index:03d}"},
        }
        for index in range(300)
    ]

    result = _node(
        f"m.selectProjectedLabels({json.dumps(candidates)}, "
        "{width:4000,height:100,cellSize:1,maxLabels:240})"
    )

    assert len(result) == 240


def test_lru_refreshes_recency_and_disposes_evictions_once() -> None:
    result = _node(
        "(()=>{const disposed=[];const c=m.createLruCache(2,v=>disposed.push(v));"
        "c.set('a',1);c.set('b',2);c.get('a');c.set('c',3);c.clear();"
        "return {disposed,size:c.size};})()"
    )

    assert result == {"disposed": [2, 1, 3], "size": 0}


def test_cad_rotation_maps_positive_y_to_negative_scene_z() -> None:
    result = _node(
        "(()=>{const angle=m.cadRotationToSceneZ(30);"
        "return {angle,x:Math.cos(angle),z:-Math.sin(angle)};})()"
    )

    assert result["angle"] == pytest.approx(math.pi / 6)
    assert result["x"] == pytest.approx(math.sqrt(3) / 2)
    assert result["z"] == pytest.approx(-0.5)


def test_spatial_index_bounds_refresh_work_for_very_large_label_sets() -> None:
    result = _node(
        "(()=>{const entries=Array.from({length:100000},(_,i)=>({"
        "worldPoint:[i%1000,Math.floor(i/1000),0],"
        "entity:{text:i%997===0?'K1+000':'note',cad_height:i%7}}));"
        "const index=m.createSpatialLabelIndex(entries,{maxCellsPerAxis:32});"
        "let visited=0;const selected=index.collect(cell=>{visited+=1;return -cell.center[0];},5000);"
        "return {cellCount:index.cellCount,visited,selected:selected.length};})()"
    )

    assert result["cellCount"] <= 32 * 32
    assert result["visited"] == result["cellCount"]
    assert result["selected"] == 5000
