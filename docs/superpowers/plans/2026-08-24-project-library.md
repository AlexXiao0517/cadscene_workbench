# Project Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a sidebar-based local project library with card/list views, safe project summaries, open/new navigation, and optimistic project renaming.

**Architecture:** Add a read-only catalog method to `ProjectService` and expose it through `GET /api/projects`. Build a separate static `project_library` application that shares navigation semantics with `project_workspace` while keeping its state and rendering code isolated.

**Tech Stack:** Python dataclasses/repositories, existing HTTP API, vanilla HTML/CSS/JavaScript, localStorage, pytest static/API contracts.

## Global Constraints

- Do not add delete, archive, export, search, user accounts, overview, or settings behavior.
- Card view is default and uses fixed-width items; it must not stretch across the row.
- Card view shows only folder icon, project name, and gray updated time.
- List view shows project name, video/CAD names, rendered clip completion, status, and updated time.
- Rename affordance appears only on project-name hover or keyboard focus-within.
- Reuse the existing project rename API and `expected_revision`.
- Do not expose absolute paths or persist project payloads in the browser.

---

### Task 1: Project catalog domain and API

**Files:**
- Create: `cadscene/projects/catalog.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/http_api.py`
- Create: `tests/projects/test_project_catalog.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Produces: `ProjectSummary.to_dict() -> dict[str, object]`.
- Produces: `ProjectService.list_projects() -> tuple[ProjectSummary, ...]`.
- Produces: `GET /api/projects -> {"projects": [...]}`.

- [ ] **Step 1: Write failing catalog tests**

Create two real repository projects with different timestamps and assets. Assert descending sort, safe filenames only, `rendered_clip_count`, `clip_count`, `running_job_count`, and status. Add a malformed project manifest directory and assert an `unavailable` entry is returned without absolute paths.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_project_catalog.py -q`

Expected: FAIL because `list_projects` is missing.

- [ ] **Step 3: Implement minimal summaries**

Scan only direct children of `projects_root`; validate IDs; load project/clips/jobs/render through existing repositories. Count a clip complete only when its latest current `clip_render` job succeeded and the render manifest owns its published output. Derive status in this order: `unavailable`, `processing`, `failed`, `completed`, `ready`, `new`. Extract only `Path(asset["path"]).name` or stored original names.

- [ ] **Step 4: Add and implement GET API test**

```python
response = api.handle("GET", "/api/projects")
assert response.status == 200
assert response.body["projects"][0]["project_id"] == "project-newer"
assert "D:\\" not in json.dumps(response.body)
```

Route GET before the existing POST create branch and return `Cache-Control: no-store`.

- [ ] **Step 5: Run focused project tests and commit**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_project_catalog.py tests/projects/test_workbench_sessions.py -q`

Commit: `feat: expose local project catalog`.

### Task 2: Project library static shell and views

**Files:**
- Create: `apps/project_library/index.html`
- Create: `apps/project_library/style.css`
- Create: `apps/project_library/project_library.js`
- Create: `tests/viewer/test_project_library_static.py`

**Interfaces:**
- Consumes: `GET /api/projects` and existing `PATCH /api/projects/{project_id}`.
- Produces: `/apps/project_library/` with card/list render functions and `mediaflow-project-library-view` localStorage key.

- [ ] **Step 1: Write failing static UI contracts**

Assert the four sidebar labels, project files active state, card/list toggle, new-project link, fixed-width grid, card fields, full table headers, empty/error/retry states, localStorage key, and hover/focus rename CSS. Assert no delete/archive/export controls.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py -q`

Expected: FAIL because the application does not exist.

- [ ] **Step 3: Implement static shell and card/list state**

Use semantic buttons/table, textContent-only rendering, URLSearchParams for project links, and `localStorage` only for `cards|list`. Use the approved line icons as inline SVG so the UI has no CDN or JavaScript icon dependency.

- [ ] **Step 4: Verify GREEN and commit**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py -q`

Commit: `feat: add card and list project library`.

### Task 3: Rename and navigation integration

**Files:**
- Modify: `apps/project_library/project_library.js`
- Modify: `apps/project_library/style.css`
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Modify: `tests/viewer/test_project_library_static.py`
- Modify: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: summary `revision`, existing PATCH response and `revision_conflict` error.
- Produces: inline rename state shared by both views; workspace “项目文件” navigation.

- [ ] **Step 1: Write failing rename/navigation contracts**

Require the rename control to be hidden by default and shown on `.project-name-row:hover` and `:focus-within`. Require PATCH body `{display_name, expected_revision}`, conflict reload, Enter/save, Escape/cancel, and project workspace navigation to `/apps/project_library/`.

- [ ] **Step 2: Verify RED and implement minimal behavior**

Run the two static test modules; implement rename with one active editor, DOM textContent, response revision refresh, and user-visible status messages. Update all five sidebar icon paths to the approved shapes while preserving existing IDs/classes.

- [ ] **Step 3: Verify GREEN and commit**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_library_static.py tests/viewer/test_project_workspace_static.py -q`

Commit: `feat: integrate project library navigation`.

### Task 4: HTTP smoke, wheel inclusion, and regression

**Files:**
- Modify: `tests/cli/test_serve_viewer_project_api.py`
- Modify: `tests/packaging/test_wheel_contents.py`
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Verifies the installed server serves the library and the real API lists a durable project.

- [ ] **Step 1: Add failing HTTP smoke**

Start the real server against `tmp_path`, create a project via POST, request GET `/api/projects`, request `/apps/project_library/index.html`, and assert the project and UI are returned.

- [ ] **Step 2: Verify RED, complete wheel resource patterns, and verify GREEN**

Run focused CLI/packaging tests. Ensure the new app is included by the existing recursive wheel resource contract.

- [ ] **Step 3: Update user documentation**

Document the project library as the normal entry for reopening projects; retain direct `projectId` URLs only as routable deep links, not required user bookkeeping.

- [ ] **Step 4: Run full verification**

```text
python -m pytest -p no:cacheprovider tests/projects tests/viewer tests/cli tests/packaging -q
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: all tests pass and the worktree is clean after commits.

- [ ] **Step 5: Commit**

Commit: `test: verify packaged project library workflow`.
