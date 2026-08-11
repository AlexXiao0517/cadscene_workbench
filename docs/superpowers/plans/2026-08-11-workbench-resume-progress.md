# Workbench Saved Progress Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reopen a completed clip at its saved render/preview state without overwriting its immutable workbench output or viewer progress.

**Architecture:** Extend the session-store abstraction with saved-session discovery, validate candidates against the current workbench context and immutable output, and carry the selected baseline into a fresh editable session. The project workbench service owns run restoration and server-side resume-stage selection.

**Tech Stack:** Python dataclasses, atomic JSON records, pytest, existing project/workbench HTTP API.

## Global Constraints

- Existing trajectory and rendering algorithms are unchanged.
- Immutable output revisions are never overwritten.
- Stale or checksum-invalid saved outputs are never resumed.
- Tests are written and observed failing before production changes.

---

### Task 1: Discover and validate a saved resume baseline

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Produces: `WorkbenchSessionStore.list_for_project(project_id) -> tuple[WorkbenchSession, ...]`
- Produces: `ProjectWorkbenchService._saved_resume_baseline(context) -> WorkbenchSession | None`

- [ ] Add an API regression test that saves a Pure Rotation workbench output, replaces the live clip reference, and expects reopening to retain the saved output revision.
- [ ] Run the focused test and verify it fails because the reopened session has no output revision.
- [ ] Add atomic-store enumeration and strict context/output validation.
- [ ] Run the focused test and verify it passes.

### Task 2: Restore the viewer run and select the resume stage

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Produces: `_restore_saved_output_to_run(session, clip) -> None`
- Produces: `_resume_workflow_stage(session) -> str`

- [ ] Add tests proving reopen does not lose saved placement, a previously overwritten run is rebuilt from the immutable artifact, and the URL uses `workflowStage=render`.
- [ ] Run the tests and verify the current implementation fails.
- [ ] Restore the validated artifact to the workflow-specific run path and derive the stage on the server.
- [ ] Run the tests and verify they pass.

### Task 3: Preserve the baseline when leaving without a new save

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Test: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Consumes: resumed session output revision/fingerprint.
- Produces: clip workbench reference state `saved` after closing an unchanged resumed session.

- [ ] Add a failing close-after-reopen test.
- [ ] Change close publication to keep a validated baseline saved.
- [ ] Run focused project/workbench tests.

### Task 4: Verify and recover the current project

**Files:**
- No production files beyond Tasks 1-3.

- [ ] Run the related project API and workbench test modules.
- [ ] Run the full test suite.
- [ ] Restart the local service on port 8312.
- [ ] Verify the current project reopens at render/preview and its saved immutable output remains referenced.
- [ ] Commit the tested fix.
