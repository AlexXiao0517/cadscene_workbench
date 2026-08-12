# CAD Text Label Size Adjustment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enlarge CAD labels by about 20% in both the Three.js CAD view and video overlay without changing performance limits or source DXF height data.

**Architecture:** Keep parsing and stored `cad_height` unchanged. Apply a presentation-only `1.2` scale to Three.js label planes and raise the two overlay base font sizes from `13/18` to `16/22`; retain all selection, throttling, candidate, mesh, and texture caps.

**Tech Stack:** Static JavaScript, Three.js, Canvas 2D, pytest static contracts, Node syntax checking.

## Global Constraints

- 3D and video overlay labels are enlarged together.
- `cad_height` in `design.json` remains unchanged.
- Active labels remain capped at 240, projected candidates at 5000, and cached textures at 384.
- Camera-driven text refresh remains throttled to 120 ms.

---

### Task 1: Enlarge presentation-only CAD text

**Files:**
- Modify: `apps/web_camera_viewer/viewer_legacy.js:17-22,355-358,1026-1029`
- Test: `tests/viewer/test_web_viewer_static.py`

**Interfaces:**
- Consumes: normalized text entities containing `cad_height` and `text_lines`.
- Produces: `CAD_TEXT_VISUAL_SCALE = 1.2`, video overlay base sizes `16/22`, and unchanged performance limits.

- [ ] **Step 1: Write the failing test**

```python
def test_cad_text_uses_approved_twenty_percent_visual_scale() -> None:
    text = _text(APP_DIR / "viewer_legacy.js")

    assert "const CAD_TEXT_VISUAL_SCALE = 1.2" in text
    assert "const base = cadHeight >= 18 ? 22 : 16" in text
    assert "charHeight * lineCount * 1.2 * CAD_TEXT_VISUAL_SCALE" in text
    assert "MAX_ACTIVE_CAD_TEXT_LABELS = 240" in text
    assert "MAX_PROJECTED_CAD_TEXT_CANDIDATES = 5000" in text
    assert "MAX_CAD_TEXT_TEXTURES = 384" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest -q tests/viewer/test_web_viewer_static.py::test_cad_text_uses_approved_twenty_percent_visual_scale`

Expected: FAIL because `CAD_TEXT_VISUAL_SCALE` is absent and overlay sizes are still `13/18`.

- [ ] **Step 3: Write minimal implementation**

```javascript
const CAD_TEXT_VISUAL_SCALE = 1.2;

function fontSizeForText(entity, highQuality) {
  const cadHeight = Number(entity.cad_height || 0);
  const base = cadHeight >= 18 ? 22 : 16;
  return Math.max(10, Math.round(base * (highQuality ? 1 : 0.72)));
}

const height = charHeight * lineCount * 1.2 * CAD_TEXT_VISUAL_SCALE;
```

- [ ] **Step 4: Run focused and regression tests**

Run: `python -m pytest -q tests/viewer/test_web_viewer_static.py tests/viewer/test_cad_text.py tests/pure_rotation/test_viewer_contract.py`

Expected: PASS.

Run: `node --check apps/web_camera_viewer/viewer_legacy.js`

Expected: exit code 0.

- [ ] **Step 5: Commit and restart test service**

```bash
git add apps/web_camera_viewer/viewer_legacy.js tests/viewer/test_web_viewer_static.py
git commit -m "fix: enlarge CAD text labels"
```

Stop only the feature-worktree server on port 8300, restart it with the feature worktree as `--root` and the main repository as `--storage-root`, then verify the portal, viewer, and `viewer_legacy.js` return HTTP 200.
