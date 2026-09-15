# Workspace Safe Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically reclaim recreatable workspace artifacts without deleting authoritative project progress or published outputs.

**Architecture:** A focused retention module owns path validation, byte accounting, diagnostic preservation, and idempotent deletion. SfM opts into exact scratch pruning after durable output publication; the project executor requests terminal-attempt pruning only after the child and log are closed, while the service validates job identity and publication boundaries. Workbench inputs use atomic hardlinks when possible and retain the existing copy fallback.

**Tech Stack:** Python 3.11, pathlib/shutil/os, dataclasses, pytest, existing ProjectService/LocalJobExecutor and atomic publication contracts.

## Global Constraints

- Work only in `D:\zjic2026\cadscene_workbench\.worktrees\workspace-retention-cleanup` on `codex/workspace-retention-cleanup`.
- Never delete source video, `dji/`, source code, Git state, keyframes, trajectories, annotations, saved workbench outputs, immutable render outputs, or merge outputs.
- Cleanup failure is best-effort and must never change a successful task into a failed task.
- Every recursive target must resolve inside its exact stage or attempt root; do not follow symlinks or reparse points.
- No manual cleanup UI and no age-based deletion of historical revisions in this release.

---

### Task 1: Retention policy primitives

**Files:**
- Create: `cadscene/projects/retention.py`
- Create: `tests/projects/test_retention.py`

**Interfaces:**
- Produces: `RetentionReport`, `prune_sfm_workspace(stage_dir: Path) -> RetentionReport`, and `reclaim_terminal_attempt(attempt_dir: Path, *, status: str, job_type: str, published_outputs: Mapping[str, str]) -> RetentionReport`.
- Consumes: only filesystem paths and immutable terminal job metadata.

- [ ] **Step 1: Write failing exact-SfM-scratch tests**

Create fixtures containing the five allowed scratch names plus formal output files, then assert `prune_sfm_workspace` removes only the scratch names, reports their bytes, rejects an outside-resolving link, and is idempotent.

- [ ] **Step 2: Verify the SfM tests fail for the missing module**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_retention.py -q`

Expected: collection fails because `cadscene.projects.retention` does not exist.

- [ ] **Step 3: Implement the minimal exact-child policy**

Implement a frozen report dataclass, root-containment checks, exact scratch-name iteration, best-effort error collection, byte accounting, and non-following recursive removal.

- [ ] **Step 4: Verify exact-SfM tests pass**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_retention.py -q`

Expected: all current retention tests pass.

- [ ] **Step 5: Write failing terminal-attempt policy tests**

Assert unsuccessful terminal attempts remove binary/media payloads while retaining small diagnostics; successful `trajectory`, `clip_export`, `sfm_solve_export`, and `project_merge` attempts remain unchanged; successful `clip_render` and `scene_bridge` are pruned only when every published path exists outside the attempt; non-terminal attempts are unchanged; a second call is harmless.

- [ ] **Step 6: Verify terminal-attempt tests fail for missing behavior**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_retention.py -q`

Expected: the new terminal-attempt assertions fail.

- [ ] **Step 7: Implement minimal terminal-attempt policy**

Use the unsuccessful terminal status whitelist and successful published-job whitelist, validate external publications, preserve bounded diagnostic suffixes, compact an oversized `adapter.log`, remove other payloads, and atomically write `retention_report.json`.

- [ ] **Step 8: Verify the retention module**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_retention.py -q`

Expected: pass.

- [ ] **Step 9: Commit**

Run: `git add cadscene/projects/retention.py tests/projects/test_retention.py docs/superpowers/specs/2026-09-02-workspace-safe-retention-design.md docs/superpowers/plans/2026-09-02-workspace-safe-retention.md && git commit -m "feat: add safe workspace retention policy"`

### Task 2: SfM lifecycle integration

**Files:**
- Modify: `cadscene/cli/run_sfm.py`
- Modify: `cadscene/projects/workflow_adapters.py`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `tests/cli/test_run_sfm_cli.py`
- Modify: `tests/projects/test_workflow_adapters.py`
- Modify: `tests/workflow/test_job_runner.py`

**Interfaces:**
- Consumes: `prune_sfm_workspace(stage_dir)` from Task 1.
- Produces: opt-in `--cleanup-workspace`, with all production SfM commands enabling it.

- [ ] **Step 1: Write failing CLI and command-construction tests**

Assert direct CLI default preserves scratch, `--cleanup-workspace` deletes scratch after formal output recording, cleanup exceptions do not change return code, and project/workflow-generated SfM commands contain the flag.

- [ ] **Step 2: Verify the new tests fail**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_run_sfm_cli.py tests/projects/test_workflow_adapters.py tests/workflow/test_job_runner.py -q`

Expected: fail because the flag and command tokens are absent.

- [ ] **Step 3: Add opt-in cleanup and production flags**

Add the parser flag; after `record_stage` succeeds call the retention function inside a best-effort boundary; append the flag to both legacy/current job-runner SfM commands and `ExistingWorkflowAdapter._sfm_command`.

- [ ] **Step 4: Verify focused SfM lifecycle tests**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_run_sfm_cli.py tests/projects/test_workflow_adapters.py tests/workflow/test_job_runner.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

Run: `git add cadscene/cli/run_sfm.py cadscene/projects/workflow_adapters.py cadscene/workflow/job_runner.py tests/cli/test_run_sfm_cli.py tests/projects/test_workflow_adapters.py tests/workflow/test_job_runner.py && git commit -m "feat: reclaim SfM scratch after publication"`

### Task 3: Terminal attempt lifecycle integration

**Files:**
- Modify: `cadscene/projects/executor.py`
- Modify: `cadscene/projects/service.py`
- Modify: `tests/projects/test_executor.py`
- Modify: `tests/projects/test_service_jobs.py`

**Interfaces:**
- Consumes: `reclaim_terminal_attempt(...)` from Task 1.
- Produces: `ProjectService.reclaim_job_attempt(project_id: str, job_id: str, *, attempt_number: int) -> RetentionReport | None` and executor best-effort invocation after log closure.

- [ ] **Step 1: Write failing service identity and policy tests**

Assert the service rejects a mismatched attempt path, preserves authoritative successful trajectory attempts, cleans a failed attempt, and cleans a successful render only when immutable published outputs exist outside the attempt.

- [ ] **Step 2: Verify service tests fail**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_service_jobs.py -q`

Expected: fail because `reclaim_job_attempt` is absent.

- [ ] **Step 3: Implement service coordination**

Load the exact queue job, validate project and attempt number/directory against `_attempt_directory`, then call the retention module without mutating queue or manifests.

- [ ] **Step 4: Verify service tests pass**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_service_jobs.py -q`

Expected: pass.

- [ ] **Step 5: Write failing executor timing and fault-isolation tests**

Use the real service and a patched coordinator method to assert cleanup observes a closed log handle, is invoked after terminal persistence, and an injected cleanup exception leaves the returned job successful.

- [ ] **Step 6: Verify executor tests fail**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_executor.py -q`

Expected: fail because the executor does not request cleanup.

- [ ] **Step 7: Invoke cleanup after resource release**

Extend the coordinator protocol and executor `finally` path: close controller/log, release the execution claim, then request cleanup inside `try/except (OSError, ValueError)` plus a defensive non-fatal exception boundary.

- [ ] **Step 8: Verify executor and service tests**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_executor.py tests/projects/test_service_jobs.py -q`

Expected: pass.

- [ ] **Step 9: Commit**

Run: `git add cadscene/projects/executor.py cadscene/projects/service.py tests/projects/test_executor.py tests/projects/test_service_jobs.py && git commit -m "feat: reclaim terminal job attempts safely"`

### Task 4: Workbench duplicate-input reduction

**Files:**
- Modify: `cadscene/projects/workbench_sessions.py`
- Modify: `tests/projects/test_workbench_sessions.py`

**Interfaces:**
- Produces: internal atomic read-only publisher that tries `os.link(source, temporary)` and falls back to `shutil.copy2(source, temporary)`.

- [ ] **Step 1: Write failing hardlink and fallback tests**

Assert workbench publication uses a hardlink for immutable video/CAD when supported, preserves exact bytes and URLs, and succeeds through copy fallback when `os.link` raises `OSError`.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -q`

Expected: hardlink expectation fails because publication always copies.

- [ ] **Step 3: Implement atomic hardlink-first publication**

Extract a private helper that creates the destination parent, links to a UUID temporary name, falls back to copy, fsyncs copied files, atomically replaces the destination, and always removes the temporary path.

- [ ] **Step 4: Verify workbench tests**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py -q`

Expected: pass, including the existing superseded-run `.stale` regression.

- [ ] **Step 5: Commit**

Run: `git add cadscene/projects/workbench_sessions.py tests/projects/test_workbench_sessions.py && git commit -m "perf: avoid duplicate workbench input copies"`

### Task 5: Full verification and integration report

**Files:**
- Modify only if a test exposes a scoped regression.

**Interfaces:**
- Produces: verified branch ready for user review; no main merge or bundle synchronization.

- [ ] **Step 1: Run focused retention regression**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_retention.py tests/cli/test_run_sfm_cli.py tests/projects/test_workflow_adapters.py tests/workflow/test_job_runner.py tests/projects/test_executor.py tests/projects/test_service_jobs.py tests/projects/test_workbench_sessions.py -q`

Expected: pass.

- [ ] **Step 2: Run the complete suite**

Run: `python -m pytest -p no:cacheprovider`

Expected: all tests pass with only existing skips/warnings.

- [ ] **Step 3: Run repository checks**

Run: `python scripts/check_no_project_dependency.py`

Expected: pass.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 4: Inspect branch state and commits**

Run: `git status --short && git log --oneline main..HEAD`

Expected: clean worktree and only the retention-related commits, including the rebased `.stale` fix.
