# Short Project Identifiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate compact collision-safe IDs for new projects and make the Windows launcher budget reflect their actual directory length.

**Architecture:** Keep project IDs as opaque stable strings, but centralize automatic generation in the project API as `p-<16 hex>`. Preserve explicit and legacy IDs. Retry only automatic creation collisions; update the launcher’s representative project path without changing job or revision contracts.

**Tech Stack:** Python 3.10, pytest, PowerShell 5 launcher.

## Global Constraints

- Existing project directories and manifests must not be renamed.
- Explicit `project_id` values must retain existing validation and behavior.
- New automatic IDs must have exactly 18 characters.
- No dependency changes.

---

### Task 1: Compact automatic project IDs

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Test: `tests/cli/test_serve_viewer_project_api.py`

**Interfaces:**
- Consumes: `ProjectApi._identity() -> str`, `validate_project_id(str) -> str`
- Produces: `ProjectApi._new_project_id() -> str`

- [ ] **Step 1: Write failing API tests**

Add tests asserting a missing `project_id` generates `p-0123456789abcdef`, an explicit ID is unchanged, and a generated collision consumes the next identity.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_serve_viewer_project_api.py -k "generated_project_id or generated_project_collision" -q`

Expected: generated ID format and collision retry tests fail.

- [ ] **Step 3: Implement minimal generation and retry**

Add `_new_project_id()` using the first 16 safe hexadecimal identity characters and retry automatic repository creation on an existing project. Leave explicit IDs on the current path.

- [ ] **Step 4: Verify GREEN**

Run the focused API tests and confirm all pass.

### Task 2: Windows path budget

**Files:**
- Modify: `packaging/windows/launcher/start.ps1`
- Test: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Consumes: automatic project ID format `p-<16 hex>`
- Produces: representative launcher path using `p-0000000000000000`

- [ ] **Step 1: Write a failing launcher assertion**

Assert the path probe contains `p-0000000000000000` and no longer contains the UUID-shaped dataset placeholder.

- [ ] **Step 2: Verify RED**

Run the focused packaging test and confirm it fails on the old placeholder.

- [ ] **Step 3: Update the launcher probe**

Replace only the representative project segment; retain the 240-character budget and the short revision directory probe.

- [ ] **Step 4: Verify GREEN and regressions**

Run the focused tests, packaging/video-analysis suites, full pytest, dependency check, and `git diff --check`.
