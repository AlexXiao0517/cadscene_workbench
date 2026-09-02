# Incomplete Route Bridge State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distinguish saved keyframes from a completed SfM route throughout adjacent scene bridging.

**Architecture:** Add one shared validated-route predicate based on the immutable output and confirmed anchor count. Apply it at capability, enqueue, target-blocking, publication, and workbench-resume boundaries while retaining immutable incomplete saves.

**Tech Stack:** Python 3, pytest, existing ProjectService and ProjectWorkbenchService repositories.

## Global Constraints

- Keep incomplete keyframe saves resumable.
- Require at least two confirmed anchors for route-source eligibility.
- Never delete immutable workbench outputs.
- Do not change SfM or scene-bridge mathematics.
- Implement behavior test-first.

---

### Task 1: Reproduce incomplete saved route state

**Files:**
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Consumes: existing workbench save and scene-bridge API helpers.
- Produces: regression coverage for incomplete source rejection and incomplete target replacement.

- [ ] Add a frame-list option to the saved-route test helper.
- [ ] Add a test proving one saved anchor remains saved but cannot source a bridge while the previous complete route can target it.
- [ ] Add a test proving successful bridging supersedes the incomplete active reference and restores the bridge seed.
- [ ] Run the new tests and confirm they fail against current behavior.

### Task 2: Implement complete-route predicate and state transitions

**Files:**
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/projects/workbench_sessions.py`

**Interfaces:**
- Consumes: immutable workbench reference and `confirmed_keyframes(track)`.
- Produces: `_validate_complete_sfm_route_output(...) -> bool` and route-aware target/source decisions.

- [ ] Add the shared predicate without changing general save validation.
- [ ] Require it for source capability and direct scene-bridge enqueue.
- [ ] Allow incomplete saved targets while continuing to block active/pending/complete targets.
- [ ] On success, stale only the matching incomplete target reference.
- [ ] Give an active scene bridge precedence over an incomplete saved baseline during workbench open.
- [ ] Run the new tests until green.

### Task 3: Regression verification

**Files:**
- No production files.

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: verified main-branch repair.

- [ ] Run all workbench-session and project-workspace tests.
- [ ] Run all project/API tests.
- [ ] Run the full pytest suite, dependency boundary check, and `git diff --check`.
- [ ] Confirm the worktree contains no workspace or runtime data changes.
