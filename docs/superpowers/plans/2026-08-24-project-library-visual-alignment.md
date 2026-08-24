# Project Library Visual Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the previous sidebar icons and align the project library with the established project workspace visual language.

**Architecture:** Keep the project library as its own static app and preserve all existing behavior. Mirror the stable workspace shell and component tokens in the library stylesheet, while reverting only the three sidebar glyphs that changed in both pages.

**Tech Stack:** HTML, CSS, vanilla JavaScript, pytest static-resource regression tests.

## Global Constraints

- Do not change project catalog, rename, navigation, card/list switching, or storage behavior.
- Do not extract a new global stylesheet or alter the stable project workspace layout.
- Use the pre-redesign file, settings, and collapse sidebar glyphs in both pages.
- Implement production changes only after observing the matching regression test fail.

---

### Task 1: Restore the existing sidebar icon language

**Files:**
- Modify: `tests/viewer/test_project_library_static.py`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_library/index.html`
- Modify: `apps/project_workspace/index.html`

**Interfaces:**
- Consumes: the existing four `data-nav` destinations and `sidebarToggle` behavior.
- Produces: identical original file, settings, and collapse glyph markup in both app shells.

- [ ] **Step 1: Write failing static tests**

Assert that both pages contain `M4 6.5h6l2 2h8v10.5H4z`, the original long gear path, and `<span aria-hidden="true">«</span>`; assert the redesigned file/settings/toggle paths are absent.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py tests/viewer/test_project_workspace_static.py -q`

Expected: FAIL because both pages currently contain the redesigned glyphs.

- [ ] **Step 3: Apply the minimal markup change**

Replace only the file, settings, and collapse glyph markup in both HTML files. Do not change labels, selected state, or navigation attributes.

- [ ] **Step 4: Verify GREEN**

Run the same focused pytest command and expect PASS.

### Task 2: Align project-library visuals with the stable workspace

**Files:**
- Modify: `tests/viewer/test_project_library_static.py`
- Modify: `apps/project_library/index.html`
- Modify: `apps/project_library/style.css`

**Interfaces:**
- Consumes: workspace theme tokens and shell dimensions from `apps/project_workspace/style.css`.
- Produces: the same visual shell, header, controls, panels, and table hierarchy while preserving library-only cards.

- [ ] **Step 1: Write failing visual-contract tests**

Assert the project library contains the workspace brand wrapper and exact stable theme/shell declarations: `--bg: #070b10`, radial body background, `228px` expanded shell, `rgba(9,15,21,.92)` sidebar, `88px` topbar, and the established primary gradient.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py -q`

Expected: FAIL because the project library currently uses a separate flat theme and dimensions.

- [ ] **Step 3: Apply minimal theme alignment**

Add the existing `brand-mark` wrapper and rewrite `apps/project_library/style.css` with the workspace tokens and shell/control/table styling. Keep the fixed 148 px project grid, folder shape, hover-only rename affordance, responsive layout, empty/error states, and all existing element IDs.

- [ ] **Step 4: Verify GREEN and regressions**

Run:

```powershell
python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py tests/viewer/test_project_workspace_static.py -q
python -m pytest -p no:cacheprovider tests/cli/test_serve_viewer_project_api.py::test_serve_viewer_serves_project_library_and_catalog_over_http -q
git diff --check
```

Expected: all tests PASS and `git diff --check` has no output.

### Task 3: Browser smoke

**Files:** No production changes expected.

**Interfaces:**
- Consumes: the running `serve_viewer` instance and the five-project acceptance storage root.
- Produces: visual confirmation that project library and project workspace look like the same application and behavior remains intact.

- [ ] **Step 1: Restart the feature-branch service**

Serve the acceptance storage root on the existing test port.

- [ ] **Step 2: Inspect both pages**

Open `/apps/project_library/` and the six-clip 杭甬高速 project workspace. Verify shared background, sidebar, topbar, controls, card/list switch, rename hover, project open, and collapse behavior.

- [ ] **Step 3: Run the full focused viewer suite**

Run: `python -m pytest -p no:cacheprovider tests/viewer -q`

Expected: PASS.
