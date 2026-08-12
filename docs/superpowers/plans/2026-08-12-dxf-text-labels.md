# DXF Text Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve DXF TEXT, MTEXT, and attached ATTRIB labels and display a bounded, readable subset in the workbench without creating Three.js resources proportional to total CAD text count.

**Architecture:** A focused Python module normalizes ezdxf text entities into the existing `design.json` layer/entity structure. A standalone browser/Node JavaScript module ranks projected labels, applies viewport and screen-grid filtering, and maintains a bounded LRU; the legacy viewer uses it to lazily create at most 240 shared-geometry text planes.

**Tech Stack:** Python 3.10+, ezdxf 1.4.4, pytest, vanilla JavaScript, Node 20, Three.js r128.

## Global Constraints

- Preserve every non-empty visible TEXT, MTEXT, and attached ATTRIB in `design.json`.
- Preserve WCS position, CAD-plane rotation, layer, effective color, character height, multiline content, and alignment.
- Do not expand ordinary block geometry or ATTDEF templates.
- Keep at most 240 active 3D labels and 384 cached text textures.
- Throttle camera-driven text selection to no more than once per 120 ms.
- Do not change Pure Rotation, SfM, project, render, or merge behavior.
- Every production behavior is introduced only after its focused test fails for the expected reason.

---

### Task 1: Normalize DXF text entities

**Files:**
- Create: `cadscene/cad/text_entities.py`
- Modify: `tests/cad/test_cad_importer.py`

**Interfaces:**
- Consumes: ezdxf `TEXT`, `MTEXT`, and attached `ATTRIB` objects plus an optional parent `INSERT`.
- Produces: `extract_text_entity(entity, parent_insert=None) -> dict[str, Any] | None` and `iter_text_entities(modelspace) -> Iterator[tuple[Any, Any | None]]`.

- [ ] **Step 1: Write failing parser-unit tests**

Add imports for `pytest`, `TextEntityAlignment`, and the new normalizer, then add:

```python
def test_text_mtext_and_block_attributes_are_normalized() -> None:
    doc = ezdxf.new("R2010")
    doc.layers.add("labels", color=2)
    msp = doc.modelspace()
    msp.add_text(
        "K12+340",
        height=2.5,
        dxfattribs={"layer": "labels", "rotation": 30},
    ).set_placement((100, 200), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_mtext(
        "第一行\\P第二行",
        dxfattribs={"layer": "labels", "char_height": 3, "rotation": 45},
    ).set_location((110, 210), attachment_point=5)
    block = doc.blocks.new("STATION_MARK")
    block.add_attdef("STA", insert=(2, 0), height=1.5, dxfattribs={"layer": "0"})
    insert = msp.add_blockref(
        "STATION_MARK", (120, 220), dxfattribs={"layer": "labels", "rotation": 90}
    )
    insert.add_auto_attribs({"STA": "ZK12+360"})
    entities = list(iter_text_entities(msp))
    labels = [extract_text_entity(entity, parent) for entity, parent in entities]

    assert {label["entity_type"] for label in labels} == {"TEXT", "MTEXT", "ATTRIB"}
    assert {label["text"] for label in labels} == {"K12+340", "第一行\n第二行", "ZK12+360"}
    assert all(label["world_position"] == label["world_points"][0] for label in labels)
    assert all(label["layer"] == "labels" for label in labels)
    text = next(label for label in labels if label["entity_type"] == "TEXT")
    assert text["cad_rotation"] == pytest.approx(30.0)
    assert text["cad_height"] == pytest.approx(2.5)
    assert (text["horizontal_align"], text["vertical_align"]) == ("center", "middle")
    mtext = next(label for label in labels if label["entity_type"] == "MTEXT")
    assert mtext["text_lines"] == 2
    assert mtext["cad_rotation"] == pytest.approx(45.0)
    attrib = next(label for label in labels if label["entity_type"] == "ATTRIB")
    assert attrib["attribute_tag"] == "STA"
    assert attrib["block_name"] == "STATION_MARK"
    assert attrib["text_role"] == "station"


def test_invisible_block_attribute_is_omitted() -> None:
    doc = ezdxf.new("R2010")
    block = doc.blocks.new("MARK")
    block.add_attdef("SECRET", insert=(0, 0), dxfattribs={"flags": 1})
    insert = doc.modelspace().add_blockref("MARK", (0, 0)).add_auto_attribs({"SECRET": "hidden"})

    assert list(iter_text_entities(doc.modelspace())) == []
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
D:\anaconda3\python.exe -m pytest tests/cad/test_cad_importer.py -k "text_mtext or block_attribute or invisible_attrib" -v
```

Expected: collection ERROR because `cadscene.cad.text_entities` does not exist.

- [ ] **Step 3: Implement the normalization module**

Create `cadscene/cad/text_entities.py` with these concrete helpers:

```python
from __future__ import annotations

import math
import re
from typing import Any, Iterator

_STATION = re.compile(r"(?:^|\\b)(?:[A-Z]{0,3})?K?\\d+\\+\\d+(?:\\.\\d+)?(?:$|\\b)", re.I)


def iter_text_entities(modelspace: Any) -> Iterator[tuple[Any, Any | None]]:
    for entity in modelspace:
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT"}:
            yield entity, None
        elif kind == "INSERT":
            for attrib in entity.attribs:
                if not attrib.is_invisible:
                    yield attrib, entity


def _xy_rotation(direction: Any) -> float:
    return math.degrees(math.atan2(float(direction.y), float(direction.x))) % 360.0


def _text_alignment(name: str) -> tuple[str, str]:
    name = name.upper()
    horizontal = "right" if name.endswith("RIGHT") else "center" if name in {"CENTER", "MIDDLE"} or name.endswith("CENTER") else "left"
    vertical = "top" if name.startswith("TOP") else "middle" if name == "MIDDLE" or name.startswith("MIDDLE") else "bottom" if name.startswith("BOTTOM") else "baseline"
    return horizontal, vertical


def _mtext_alignment(value: int) -> tuple[str, str]:
    row, column = divmod(max(1, min(9, value)) - 1, 3)
    return ("left", "center", "right")[column], ("top", "middle", "bottom")[row]


def _effective_layer(entity: Any, parent_insert: Any | None) -> str:
    layer = str(entity.dxf.layer)
    return str(parent_insert.dxf.layer) if parent_insert is not None and layer == "0" else layer


def extract_text_entity(entity: Any, parent_insert: Any | None = None) -> dict[str, Any] | None:
    kind = entity.dxftype()
    if kind in {"TEXT", "ATTRIB"}:
        text = entity.plain_text().strip()
        if not text:
            return None
        align, point, second = entity.get_placement()
        position = entity.ocs().to_wcs(point)
        if second is not None:
            second_wcs = entity.ocs().to_wcs(second)
            direction = second_wcs - position
        else:
            angle = math.radians(float(entity.dxf.get("rotation", 0.0)))
            direction = entity.ocs().to_wcs((math.cos(angle), math.sin(angle), 0.0))
        horizontal, vertical = _text_alignment(align.name)
        height = float(entity.dxf.get("height", 1.0))
    elif kind == "MTEXT":
        text = entity.plain_text(split=False, fast=False).strip()
        if not text:
            return None
        position = entity.dxf.insert
        direction = entity.ucs().ux
        horizontal, vertical = _mtext_alignment(int(entity.dxf.get("attachment_point", 1)))
        height = float(entity.dxf.get("char_height", 1.0))
    else:
        return None
    layer = _effective_layer(entity, parent_insert)
    item = {
        "entity_id": str(getattr(entity.dxf, "handle", "") or kind),
        "entity_type": kind,
        "type": "text",
        "text": text,
        "layer": layer,
        "world_position": [float(position.x), float(position.y), float(position.z)],
        "world_points": [[float(position.x), float(position.y), float(position.z)]],
        "cad_rotation": _xy_rotation(direction),
        "cad_height": max(height, 1e-9),
        "text_lines": len(text.splitlines()) or 1,
        "horizontal_align": horizontal,
        "vertical_align": vertical,
        "text_role": "station" if _STATION.search(text) or any(token in layer.lower() for token in ("桩号", "station", "chainage")) else "annotation",
    }
    if kind == "ATTRIB":
        item["attribute_tag"] = str(entity.dxf.tag)
        item["block_name"] = str(parent_insert.dxf.name) if parent_insert is not None else ""
    return item
```

- [ ] **Step 4: Run the normalizer tests and verify GREEN**

Run the Step 2 command again. Expected: the new direct normalizer tests PASS.

- [ ] **Step 5: Commit the independently tested normalizer**

```powershell
git add cadscene/cad/text_entities.py tests/cad/test_cad_importer.py
git commit -m "test: define DXF text normalization"
```

### Task 2: Integrate text into design.json and import statistics

**Files:**
- Modify: `cadscene/cad/dxf_parser.py`
- Modify: `cadscene/cad/importer.py`
- Modify: `tests/cad/test_cad_importer.py`
- Modify: `tests/workflow/test_data_import.py`

**Interfaces:**
- Consumes: `iter_text_entities()` and `extract_text_entity()` from Task 1.
- Produces: text entities inside `design["layers"][*]["entities"]`, plus `text_count` and `text_entity_types` statistics.

- [ ] **Step 1: Add failing effective-color and text-only DXF tests**

Add these integration tests:

```python
def test_parse_dxf_integrates_text_entities_and_statistics(tmp_path: Path) -> None:
    source = tmp_path / "labels.dxf"
    doc = ezdxf.new("R2010")
    doc.layers.add("labels", color=2)
    doc.modelspace().add_text("K1+020", height=2, dxfattribs={"layer": "labels"})
    doc.modelspace().add_mtext("说明\\P第二行", dxfattribs={"layer": "labels", "char_height": 3})
    doc.saveas(source)

    design, stats = parse_dxf(source)
    labels = [entity for layer in design["layers"] for entity in layer["entities"]]

    assert {label["entity_type"] for label in labels} == {"TEXT", "MTEXT"}
    assert stats["text_count"] == 2
    assert stats["text_entity_types"] == {"MTEXT": 1, "TEXT": 1}
    assert stats["segment_count"] == 0


def test_text_only_dxf_completes_import(tmp_path: Path) -> None:
    source = tmp_path / "text-only.dxf"
    doc = ezdxf.new("R2010")
    doc.modelspace().add_text("K0+000", height=2).set_placement((10, 20))
    doc.saveas(source)

    result = import_dxf(source, tmp_path / "dataset")

    assert result.stats["text_count"] == 1
    assert result.stats["bbox"] == [10.0, 20.0, 10.0, 20.0]
    assert "文字标注数量" in result.report.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run and verify RED**

```powershell
D:\anaconda3\python.exe -m pytest tests/cad/test_cad_importer.py tests/workflow/test_data_import.py -k "text or attrib" -v
```

Expected: FAIL on missing entities/statistics and the text-only ValueError.

- [ ] **Step 3: Integrate text before geometry extraction**

In `parse_dxf()`:

```python
from cadscene.cad.text_entities import extract_text_entity, iter_text_entities

text_type_counts: Counter[str] = Counter()
for entity, parent in iter_text_entities(document.modelspace()):
    try:
        item = extract_text_entity(entity, parent)
    except Exception:
        unsupported[entity.dxftype()] += 1
        continue
    if item is None:
        continue
    layer_name = decode_legacy_dxf_text(item["layer"])
    item["layer"] = layer_name
    style_entity = parent if int(getattr(entity.dxf, "color", 256) or 256) == 0 and parent is not None else entity
    aci = _resolve_aci(style_entity, document)
    color = _entity_color(style_entity, document, colors, aci)
    item.update({"aci_color": aci, "color": color, "bbox": _bbox(item["world_points"])})
    layer = layers[layer_name]
    layer.setdefault("name", layer_name)
    layer.setdefault("kind", _layer_kind(layer_name))
    layer.setdefault("aci_color", aci)
    layer.setdefault("color", color)
    layer["entities"].append(item)
    text_type_counts[item["entity_type"]] += 1
    aci_colors.add(aci)
    all_points.extend(item["world_points"])
```

Keep the geometry loop unchanged except that `segment_count` reads `entity.get("closed", False)`. Add:

```python
"text_count": sum(text_type_counts.values()),
"text_entity_types": dict(sorted(text_type_counts.items())),
```

to parse statistics. Extend the import report with the text count and type summary.

- [ ] **Step 4: Run focused CAD/import tests and verify GREEN**

```powershell
D:\anaconda3\python.exe -m pytest tests/cad/test_cad_importer.py tests/workflow/test_data_import.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit parser integration**

```powershell
git add cadscene/cad/dxf_parser.py cadscene/cad/importer.py tests/cad/test_cad_importer.py tests/workflow/test_data_import.py
git commit -m "feat: preserve DXF text and block attributes"
```

### Task 3: Implement bounded browser label selection

**Files:**
- Create: `apps/web_camera_viewer/cad_text.js`
- Create: `tests/viewer/test_cad_text.py`
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `tests/viewer/test_web_viewer_static.py`

**Interfaces:**
- Produces browser global/CommonJS API `CadsceneCadText` with `isStationLabel`, `selectProjectedLabels`, and `createLruCache`.
- `selectProjectedLabels(candidates, options)` returns ranked candidate objects without mutating input.

- [ ] **Step 1: Write Node-backed failing tests**

Create `tests/viewer/test_cad_text.py` with a Node helper matching the existing pure-rotation tests and these assertions:

```python
MODULE = Path("apps/web_camera_viewer/cad_text.js")


def _node(expression: str):
    script = f"const m=require({json.dumps(str(MODULE.resolve()))}); console.log(JSON.stringify({expression}));"
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_selection_rejects_offscreen_and_prefers_station_in_same_cell() -> None:
    candidates = [
        {"x": 10, "y": 10, "depth": 0, "entity": {"entity_id": "note", "text": "说明", "cad_height": 20}},
        {"x": 12, "y": 12, "depth": 0, "entity": {"entity_id": "station", "text": "K1+020", "cad_height": 2}},
        {"x": 500, "y": 500, "depth": 0, "entity": {"entity_id": "outside", "text": "K9+999"}},
    ]
    result = _node(f"m.selectProjectedLabels({json.dumps(candidates)}, {{width:100,height:100,cellSize:50,maxLabels:240,margin:0}})")
    assert [row["entity"]["entity_id"] for row in result] == ["station"]


def test_selection_hard_caps_labels() -> None:
    candidates = [{"x": i * 10, "y": 0, "depth": 0, "entity": {"entity_id": str(i), "text": f"K0+{i:03d}"}} for i in range(300)]
    result = _node(f"m.selectProjectedLabels({json.dumps(candidates)}, {{width:4000,height:100,cellSize:1,maxLabels:240}})")
    assert len(result) == 240


def test_lru_refreshes_recency_and_disposes_evictions_once() -> None:
    result = _node("(()=>{const disposed=[];const c=m.createLruCache(2,v=>disposed.push(v));c.set('a',1);c.set('b',2);c.get('a');c.set('c',3);c.clear();return {disposed,size:c.size};})()")
    assert result == {"disposed": [2, 1, 3], "size": 0}
```

- [ ] **Step 2: Run and verify RED**

```powershell
D:\anaconda3\python.exe -m pytest tests/viewer/test_cad_text.py tests/viewer/test_web_viewer_static.py -v
```

Expected: FAIL because `cad_text.js` and its script tag do not exist.

- [ ] **Step 3: Implement the pure JavaScript module**

Create a UMD module with:

```javascript
function isStationLabel(label) {
  const text = String(label?.text || "");
  const layer = String(label?.layer || "");
  return label?.text_role === "station"
    || /(?:^|\b)(?:[A-Z]{0,3})?K?\d+\+\d+(?:\.\d+)?(?:$|\b)/i.test(text)
    || /(桩号|station|chainage)/i.test(layer);
}

function selectProjectedLabels(candidates, options = {}) {
  const width = Math.max(Number(options.width) || 1, 1);
  const height = Math.max(Number(options.height) || 1, 1);
  const cellSize = Math.max(Number(options.cellSize) || 84, 1);
  const maxLabels = Math.max(0, Number(options.maxLabels) || 240);
  const margin = Number(options.margin ?? 0.08);
  const ranked = candidates.filter((candidate) =>
    Number.isFinite(candidate.x) && Number.isFinite(candidate.y) && candidate.depth >= -1 && candidate.depth <= 1
    && candidate.x >= -margin * width && candidate.x <= (1 + margin) * width
    && candidate.y >= -margin * height && candidate.y <= (1 + margin) * height
  ).map((candidate) => ({
    ...candidate,
    _priority: (isStationLabel(candidate.entity) ? 1_000_000 : 0)
      + Math.max(0, Number(candidate.entity?.cad_height) || 0) * 1_000
      - Math.hypot(candidate.x - width / 2, candidate.y - height / 2),
  })).sort((a, b) => b._priority - a._priority);
  const cells = new Set();
  const selected = [];
  for (const candidate of ranked) {
    const key = `${Math.floor(candidate.x / cellSize)}:${Math.floor(candidate.y / cellSize)}`;
    if (cells.has(key)) continue;
    cells.add(key);
    selected.push(candidate);
    if (selected.length >= maxLabels) break;
  }
  return selected.map(({ _priority, ...candidate }) => candidate);
}
```

Implement `createLruCache(limit, dispose)` with `get`, `set`, `delete`, `clear`, and `size`; `get` refreshes recency and evictions call `dispose` exactly once.

- [ ] **Step 4: Load the module before viewer_legacy.js and verify GREEN**

Add `<script src="./cad_text.js?v=20260812-dxf-text-v1"></script>` after `video_display_transform.js` and before `viewer_legacy.js`. Run Step 2; expected PASS.

- [ ] **Step 5: Commit selection module**

```powershell
git add apps/web_camera_viewer/cad_text.js apps/web_camera_viewer/index.html tests/viewer/test_cad_text.py tests/viewer/test_web_viewer_static.py
git commit -m "feat: bound visible CAD text selection"
```

### Task 4: Replace eager Three.js text creation with a lazy bounded pool

**Files:**
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `tests/viewer/test_web_viewer_static.py`
- Modify: `tests/pure_rotation/test_viewer_contract.py`

**Interfaces:**
- Consumes: `window.CadsceneCadText.selectProjectedLabels()` and `createLruCache()`.
- Produces: `threeScene.setCadTextVisible()` and internal throttled `refreshCadTextLabels()` with at most 240 active meshes.

- [ ] **Step 1: Write failing static/runtime-contract tests**

Add:

```python
def test_cad_text_rendering_is_lazy_bounded_and_throttled() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")
    assert "MAX_ACTIVE_CAD_TEXT_LABELS = 240" in text
    assert "MAX_CAD_TEXT_TEXTURES = 384" in text
    assert "CAD_TEXT_REFRESH_MS = 120" in text
    assert "cadTextGeometry" in text
    assert "createLruCache(MAX_CAD_TEXT_TEXTURES" in text
    assert "selectProjectedLabels" in text
    assert "orbitControls.addEventListener(\"change\"" in text
    initial_loop = text[text.index("for (const layer of data.layers || [])"):text.index("for (const [color, vertices]")]
    assert "makeTextTexture" not in initial_loop
```

- [ ] **Step 2: Run and verify RED**

```powershell
D:\anaconda3\python.exe -m pytest tests/viewer/test_web_viewer_static.py tests/pure_rotation/test_viewer_contract.py -k "cad_text or valid_javascript or script" -v
```

Expected: FAIL because viewer text creation is eager and unbounded.

- [ ] **Step 3: Implement the bounded 3D label pool**

In `createThreeScene()`:

- collect text entities into `cadTextEntities` while keeping the existing color-batched line loop;
- create one `PlaneGeometry(1, 1)` and one group for active labels;
- project all text positions with a reused `THREE.Vector3`, call `selectProjectedLabels`, and diff by `entity_id`;
- create only newly selected meshes, sharing `cadTextGeometry` and using cached CanvasTextures;
- set mesh scale from CAD height, multiline count, and measured aspect ratio;
- apply local horizontal/vertical anchor offset before CAD rotation;
- dispose evicted textures through the LRU;
- schedule refresh with one timer at a 120 ms minimum interval from `orbitControls` change events;
- preserve `setCadTextVisible()` without traversing all CAD lines.

Update `makeTextTexture()` to support newline-separated lines, return texture aspect metadata, and use one consistent black-outline/color-fill style.

- [ ] **Step 4: Reuse bounded selection for the video overlay**

Before drawing text, project text candidates once, call `selectProjectedLabels` with overlay dimensions and `maxLabels: 240`, and draw only the returned entity IDs. Keep line overlays unchanged and preserve `showCadText`.

- [ ] **Step 5: Run viewer and JavaScript verification**

```powershell
node --check apps/web_camera_viewer/cad_text.js
node --check apps/web_camera_viewer/viewer_legacy.js
D:\anaconda3\python.exe -m pytest tests/viewer tests/pure_rotation/test_viewer_contract.py -v
```

Expected: both syntax checks and all selected tests PASS.

- [ ] **Step 6: Commit viewer integration**

```powershell
git add apps/web_camera_viewer/viewer_legacy.js tests/viewer/test_web_viewer_static.py tests/pure_rotation/test_viewer_contract.py
git commit -m "feat: render DXF labels with bounded Three.js resources"
```

### Task 5: Regression verification and scope audit

**Files:**
- Modify only if a regression test exposes a defect in files already listed above.

**Interfaces:**
- Produces a verified feature branch containing only DXF text-label work.

- [ ] **Step 1: Run focused CAD and viewer suites**

```powershell
D:\anaconda3\python.exe -m pytest tests/cad tests/viewer tests/workflow/test_data_import.py tests/cli/test_serve_viewer_upload_api.py -q
```

- [ ] **Step 2: Run JavaScript syntax checks**

```powershell
node --check apps/web_camera_viewer/cad_text.js
node --check apps/web_camera_viewer/viewer_legacy.js
```

- [ ] **Step 3: Run the full suite**

```powershell
D:\anaconda3\python.exe -m pytest -q
```

Expected: all tracked tests pass with only existing skips/warnings.

- [ ] **Step 4: Audit branch scope and parent worktree isolation**

```powershell
git status --short
git diff --check b8e4908..HEAD
git diff --stat b8e4908..HEAD
git -C D:\zjic2026\cadscene_workbench status --short --branch
```

Expected: feature worktree clean; only design/plan, CAD parser/tests, and viewer/tests are changed; the parent worktree retains its pre-existing Pure Rotation, SOP, and weekly-report modifications.

- [ ] **Step 5: Request independent code review and address Critical/Important findings**

Use base `b8e4908` and current `HEAD`, then rerun the relevant focused and full verification after any fix.
