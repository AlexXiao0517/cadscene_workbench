# Compact Windows Publication Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every generated analysis publication path safely below the legacy Windows path limit while preserving full content verification and old-project compatibility.

**Architecture:** The API generates compact project IDs, the portal consumes the returned ID, and the publisher separates full logical SHA identities from compact physical directory names. Short temporary directories preserve the existing atomic copy/verify/replace workflow.

**Tech Stack:** Python 3.11, pathlib, tempfile, SHA-256/base32, vanilla JavaScript, pytest, PowerShell.

## Global Constraints

- Do not modify or merge `main`.
- Do not delete or overwrite any existing extracted tester bundle.
- Existing long project IDs and recorded artifact paths remain readable.
- Preserve atomic publication and full SHA-256 verification.

---

### Task 1: Server-owned compact project ID

**Files:**
- Modify: `apps/workflow_portal/portal_api.js`
- Modify: `apps/workflow_portal/workflow_portal.js`
- Modify: `apps/workflow_portal/index.html`
- Test: `tests/viewer/test_workflow_portal_static.py`

- [ ] Add a failing static test proving the portal omits `project_id` and adopts the API response ID.
- [ ] Run the focused test and confirm it fails for the client-generated UUID.
- [ ] Remove `generatedId()`, let the API allocate the ID, and bump the asset version.
- [ ] Run the focused portal tests.

### Task 2: Compact immutable publication paths

**Files:**
- Modify: `cadscene/projects/analysis_publication.py`
- Test: `tests/projects/test_analysis_jobs.py`

- [ ] Add a failing publisher test using a legacy `dataset-UUID` project ID.
- [ ] Confirm the test exposes full-hash artifact and staging directory names.
- [ ] Add compact physical artifact naming and short `.pub-` staging directories while retaining the full logical artifact ID.
- [ ] Update path/name assertions and verify fingerprint collision behavior remains safe.
- [ ] Run focused analysis publication tests.

### Task 3: Launcher path budget

**Files:**
- Modify: `packaging/windows/launcher/start.ps1`
- Test: `tests/packaging/test_windows_offline_bundle.py`

- [ ] Add a failing test requiring a legacy project ID and the compact artifact publication path in the probe.
- [ ] Update the launcher probe to model the real job, staging, and final artifact paths.
- [ ] Run packaging and PowerShell parser tests.

### Task 4: Verification and package

**Files:**
- Generated: a new bundle staging directory and `CADScene-0.1.0.zip` in a new output location.

- [ ] Run focused API, portal, publication, video-analysis, and packaging suites.
- [ ] Run the complete pytest suite, dependency check, and `git diff --check`.
- [ ] Build and install the wheel into the isolated packaged runtime.
- [ ] Assemble and verify a new offline bundle without touching existing extracted copies.
- [ ] Inspect the ZIP members and report the absolute package path, size, checksum, branch, and commit.
