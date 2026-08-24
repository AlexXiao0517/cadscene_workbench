# Project Shell Theme Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent dark/light theme switch to the project library and project workspace sidebars.

**Architecture:** Reuse the established `mediaflow-theme` localStorage contract and the upload/workbench sun/moon semantics. Each static app owns a small theme initializer, while CSS custom properties provide complete light and dark palettes without changing project behavior.

**Tech Stack:** HTML, CSS custom properties, vanilla JavaScript, pytest static-resource tests.

## Global Constraints

- Add the switch to both project-library and project-workspace pages.
- Persist only `light` or `dark` under `mediaflow-theme`.
- Respect `prefers-color-scheme` when no saved value exists.
- Place the workspace switch above “当前项目” and the library switch above “收起侧栏”.
- Keep all project, job, rename, navigation, card/list, and polling behavior unchanged.

---

### Task 1: Add the shared sidebar interaction contract

**Files:**
- Modify: `tests/viewer/test_project_library_static.py`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_library/index.html`
- Modify: `apps/project_library/project_library.js`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`

- [ ] Write failing tests for the switch markup, placement, `mediaflow-theme`, saved/system initialization, `data-theme`, and accessible label updates.
- [ ] Run the two static test files and verify they fail because the switch is absent.
- [ ] Add the same sun/moon markup and minimal initializer/click handler to both pages.
- [ ] Re-run the focused tests and verify the interaction contract passes.

### Task 2: Add complete project-shell light palettes

**Files:**
- Modify: `tests/viewer/test_project_library_static.py`
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_library/style.css`
- Modify: `apps/project_workspace/style.css`

- [ ] Write failing tests for `:root[data-theme="light"]`, the approved light palette, switch icon state, and theme-backed page/sidebar/topbar/panel surfaces.
- [ ] Run the focused tests and verify they fail because both pages are dark-only.
- [ ] Introduce semantic surface variables, light values, switch layout, collapsed behavior, and icon visibility without changing geometry.
- [ ] Run focused Viewer tests, full pytest, dependency check, and `git diff --check`.

### Task 3: Keep the feature branch available for UI acceptance

**Files:** No production changes expected.

- [ ] Confirm project library and the six-clip project workspace return HTTP 200 on the running service.
- [ ] Commit the theme feature without merging or pushing.
- [ ] Keep the service and worktree running for user acceptance.
