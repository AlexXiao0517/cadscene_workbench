# Render Workflow Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the viewer flow from quality acceptance to render export while hiding unused review controls.

**Architecture:** Keep the legacy viewer and `JobRunner` intact. `workflow.js` owns stage selection and render artifact readiness; `index.html` supplies one transition button and marks dormant legacy review controls hidden. The existing render stage continues to invoke `cadscene.cli.render_overlay`.

**Tech Stack:** Static HTML/CSS/JavaScript, Python pytest, existing local `serve_viewer` job API.

## Global Constraints

- Do not modify `project/`.
- Do not import `project` or `cadvideo`.
- Do not add npm, React, Vite, or node_modules.
- Preserve the legacy left video/right Three.js layout and bottom UAV camera controls.
- Do not alter alignment, quality, or overlay rendering algorithms.

---

### Task 1: Add the Quality-to-Render Transition and Hide Dormant Review Controls

**Files:**
- Modify: `apps/web_camera_viewer/index.html`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `apps/web_camera_viewer/style.css`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: `setWorkflowStage(stage)` in `workflow.js`.
- Produces: `#workflowFinishQuality` and default-hidden legacy review controls.

- [ ] **Step 1: Write the failing static test**

```python
def test_quality_can_finish_into_render_and_dormant_review_controls_are_hidden() -> None:
    html = _read("index.html")
    workflow = _read("workflow.js")
    assert 'id="workflowFinishQuality"' in html
    assert 'setWorkflowStage("render")' in workflow
    for control_id in ("reviewStatus", "acceptPrediction", "saveAdjustedKeyframe", "rejectReview"):
        assert f'id="{control_id}"' in html
    assert "workflow-hidden-control" in html
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: FAIL because `workflowFinishQuality` is absent.

- [ ] **Step 3: Implement the minimum HTML, CSS, and event handler**

```javascript
document.querySelector("#workflowFinishQuality")?.addEventListener("click", () => {
  setWorkflowStage("render");
});
```

Use the existing `.dev-only-control` hiding pattern or a dedicated `.workflow-hidden-control { display: none; }` class on the four dormant review elements. Leave their legacy IDs and event handlers intact.

- [ ] **Step 4: Run the focused test**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: PASS.

### Task 2: Refresh Render Availability After Job Completion

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: `runPath("08_render/sfm_align_overlay.mp4")`, `renderStatus(payload)`.
- Produces: `refreshRenderOutputState()` which enables preview/download only when the output exists.

- [ ] **Step 1: Write the failing static test**

```python
def test_render_panel_refreshes_output_after_render_job_success() -> None:
    script = _read("workflow.js")
    assert "refreshRenderOutputState" in script
    assert 'operation === "render"' in script or 'operation !== "render"' in script
    assert 'method: "HEAD"' in script
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py -q`

Expected: FAIL because `refreshRenderOutputState` is absent.

- [ ] **Step 3: Implement the minimum render state helper**

```javascript
async function refreshRenderOutputState() {
  const response = await fetch(renderPath, { method: "HEAD", cache: "no-store" });
  preview.disabled = !response.ok;
  download.toggleAttribute("aria-disabled", !response.ok);
}
```

Call it on initial load and once when `renderStatus` observes a successful `render` operation. On success, retain the selected render stage and show a short availability message.

- [ ] **Step 4: Run focused static and API tests**

Run: `python -m pytest tests/viewer/test_workflow_ui_static.py tests/cli/test_serve_viewer_workflow_api.py tests/cli/test_render_overlay_cli.py -q`

Expected: PASS.

### Task 3: Verify the Full Viewer and Render Regression Surface

**Files:**
- Modify: `apps/web_camera_viewer/index.html` only if script cache versions require a bump.
- Test: existing viewer, workflow, rendering, CLI, and dependency tests.

- [ ] **Step 1: Validate JavaScript syntax**

Run:

```powershell
node --check apps/web_camera_viewer/workflow.js
node --check apps/web_camera_viewer/viewer_legacy.js
```

Expected: both commands exit 0.

- [ ] **Step 2: Run the full regression command**

Run:

```powershell
python -m pytest tests/core tests/cad tests/sfm tests/alignment tests/viewer tests/workflow tests/diagnostics tests/rendering tests/cli/test_align_to_cad_cli.py tests/cli/test_evaluate_quality_cli.py tests/cli/test_export_viewer_scene_cli.py tests/cli/test_analyze_road_surface_cli.py tests/cli/test_render_overlay_cli.py tests/cli/test_serve_viewer_cli.py tests/cli/test_serve_viewer_workflow_api.py tests/cli/test_run_pipeline.py tests/cli/test_run_sfm_cli.py tests/scripts/test_check_no_project_dependency.py
python scripts/check_no_project_dependency.py
```

Expected: all tests pass and the dependency checker reports no forbidden runtime dependency.
