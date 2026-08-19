# Route Fit Anchor Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an unconfirmed Viewer seed pose or a high-residual multi-anchor fit from being published as a valid CAD route.

**Architecture:** Define one Python selector for explicitly confirmed keyframe sources and reuse it in alignment, keyframe-plan progress, and workflow command validation. Mirror the same selector in the browser so displayed counts and preflight validation match the backend, while retaining compatibility for legacy tracks whose multiple poses all predate the `source` field. Keep the existing Sim3 solver unchanged and retain the 5 m global residual gate for every scale-observable result.

**Tech Stack:** Python 3, pytest, browser JavaScript static-contract tests.

## Global Constraints

- Do not modify SfM, Sim3 fitting math, SRT, or Pure Rotation behavior.
- Preserve existing explicit manual sources: `manual_keyframe`, `confirmed_keyframe`, `manual`, `confirmed`, `manual_anchor`, and `manual_corrected`.
- Treat a lone missing-source pose, unknown sources, and `algorithm_prediction` as non-authoritative; preserve a legacy all-source-less track only when it contains at least two poses.
- Keep all work isolated from the dirty `main` worktree.

---

### Task 1: Authoritative keyframe classification

**Files:**
- Modify: `cadscene/alignment/keyframes.py`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `cadscene/workflow/keyframe_plan.py`
- Test: `tests/alignment/test_keyframes.py`
- Test: `tests/workflow/test_job_runner.py`
- Test: `tests/workflow/test_keyframe_plan.py`

**Interfaces:**
- Produces: `is_confirmed_keyframe(item: Mapping[str, object]) -> bool`
- Consumes: camera-track entries containing `source`, `frame`, and `camera`.

- [x] Add failing tests proving a source-less frame-0 seed is excluded from alignment, task preflight counts, and plan completion.
- [x] Run the three focused tests and confirm they fail because the seed is currently counted.
- [x] Add the shared explicit-source predicate and replace the duplicated permissive checks.
- [x] Run the focused tests and confirm they pass.

### Task 2: Browser preflight and residual regression

**Files:**
- Modify: `apps/web_camera_viewer/viewer_legacy.js`
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `tests/viewer/test_workflow_ui_static.py`
- Modify: `tests/alignment/test_aligner.py`

**Interfaces:**
- Consumes: the same explicit manual source values as the backend predicate.
- Produces: UI counts and alignment preflight that exclude the unconfirmed frame-0 seed.

- [x] Add failing browser contract assertions for an explicit source set and add a 3-keyframe, over-5 m residual rejection test.
- [x] Run the focused tests and confirm the browser contract fails before implementation while the residual test protects the committed validator.
- [x] Replace permissive JavaScript source checks with the explicit-source helper; do not alter the solver.
- [x] Run all focused tests, then the full suite, dependency check, and `git diff --check`.
