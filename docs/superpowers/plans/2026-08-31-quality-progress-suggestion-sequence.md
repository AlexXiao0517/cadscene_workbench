# Quality Progress and Suggestion Sequence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish truthful quality-pipeline progress and browse suggested frames chronologically in a looping sequence.

**Architecture:** The quality child process writes atomic progress sidecars from measured frame work and real pipeline stage boundaries. `JobRunner` mirrors monotonic sidecar updates into the existing authoritative `job_status.json`. A small DOM-free JavaScript module owns suggestion sorting and cursor movement while `workflow.js` remains responsible for persistence and video navigation.

**Tech Stack:** Python 3.11, pytest, subprocess/atomic JSON, browser JavaScript, Node.js contract tests.

## Global Constraints

- Do not use timer-driven or elapsed-time simulated progress.
- Do not report 100% before the parent process validates successful completion.
- Preserve current quality algorithms, thresholds, PTS authority, and output files.
- Suggestions browse by ascending `frame_index`, loop after the last item, and advance after ignore.
- Do not modify or include any up/down scene-bridge functionality.
- Merge to `main`, push, and rebuild the Windows 0.1.2 full bundle only after focused and full regression pass.

---

### Task 1: Pure suggestion sequence module

**Files:**
- Create: `apps/web_camera_viewer/suggestion_sequence.js`
- Modify: `apps/web_camera_viewer/index.html`
- Create: `tests/viewer/test_suggestion_sequence.py`

**Interfaces:**
- Produces: `CadsceneSuggestionSequence.ordered(items)`, `next(items, currentFrame)`, and `firstAfter(items, frame)`.
- Consumes: suggestion objects with integer-like `frame_index`.

- [ ] **Step 1: Write failing Node-backed tests**

Test ascending order, first selection, next selection, last-to-first looping, and `firstAfter` after a removed final/middle frame by requiring the browser module from Node.

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_suggestion_sequence.py -q`

Expected: FAIL because `suggestion_sequence.js` does not exist.

- [ ] **Step 3: Implement the minimal DOM-free module and load it before workflow.js**

The module must export through both `window.CadsceneSuggestionSequence` and `module.exports`, copy rather than mutate input arrays, coerce frame indices with `Number`, and loop deterministically.

- [ ] **Step 4: Run the tests and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_suggestion_sequence.py tests/viewer/test_workflow_ui_static.py -q`

- [ ] **Step 5: Commit**

Commit message: `feat: add chronological suggestion sequence`

### Task 2: Integrate looping suggestion navigation

**Files:**
- Modify: `apps/web_camera_viewer/workflow.js`
- Modify: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: `window.CadsceneSuggestionSequence` from Task 1.
- Preserves: `/api/workflow/ignore-suggestion` persistence and ignored frame restoration.

- [ ] **Step 1: Write failing UI contract tests**

Assert that loaded suggestions use `ordered`, view clicks use `next`, ignore uses `firstAfter`, the selected suggestion is jumped immediately after ignore, and the button copy contains the next one-based position and total.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_workflow_ui_static.py -q`

Expected: FAIL because the old risk-score sort and current-frame reuse remain.

- [ ] **Step 3: Implement minimal integration**

Replace risk-score display sorting with chronological ordering, extract a `jumpToSuggestion` UI helper, advance with `next`, update button copy from the next candidate, and after ignore jump to `firstAfter` with wraparound.

- [ ] **Step 4: Run and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_suggestion_sequence.py tests/viewer/test_workflow_ui_static.py -q`

- [ ] **Step 5: Commit**

Commit message: `fix: browse quality suggestions in sequence`

### Task 3: Publish measured quality progress

**Files:**
- Modify: `cadscene/alignment/quality.py`
- Modify: `cadscene/cli/evaluate_quality.py`
- Modify: `cadscene/cli/run_pipeline.py`
- Modify: `cadscene/workflow/job_runner.py`
- Modify: `tests/alignment/test_quality.py`
- Modify: `tests/cli/test_evaluate_quality_cli.py`
- Modify: `tests/cli/test_run_pipeline.py`
- Modify: `tests/workflow/test_job_runner.py`

**Interfaces:**
- `evaluate_quality(..., progress_callback: Callable[[int, int], None] | None = None)` reports processed and total authoritative path rows.
- `evaluate_quality --progress-file PATH --progress-start FLOAT --progress-end FLOAT` maps measured rows into an overall interval.
- `run_pipeline --progress-file PATH` writes real substage milestones through `cadscene.cli._progress.write_progress_sidecar`.
- `JobRunner` reads the progress file named in the child command and mirrors only strictly non-decreasing fractions below 1.0 while the process is running.

- [ ] **Step 1: Write failing frame-progress tests**

Assert the quality evaluator reports `(1, total)` through `(total, total)` and the CLI sidecar reaches its configured interval end without reaching 1.0.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/alignment/test_quality.py tests/cli/test_evaluate_quality_cli.py -q`

- [ ] **Step 3: Implement measured frame reporting**

Add the optional callback, emit once per processed row, add CLI mapping arguments, and atomically write Chinese stage messages through the shared sidecar helper.

- [ ] **Step 4: Write failing pipeline milestone tests**

Assert quality preparation starts above 5%, quality output reaches 55%, Viewer scene reaches 75%, road-surface completion/skip reaches 95%, and no child milestone reaches 100%.

- [ ] **Step 5: Run and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_run_pipeline.py -q`

- [ ] **Step 6: Implement real substage milestones**

Pass the same progress file into `evaluate_quality`, write start/completion messages around each selected substage, and preserve skip behavior.

- [ ] **Step 7: Write failing JobRunner sidecar tests**

Run a child command that atomically writes progress then sleeps. Assert `job_status.json` observes the intermediate fraction, ignores a later lower fraction, remains below 1.0 while running, and becomes exactly 1.0 only after successful exit.

- [ ] **Step 8: Run and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/workflow/test_job_runner.py -q`

- [ ] **Step 9: Implement monotonic sidecar mirroring**

Change the monitor from one blocking `wait()` to a bounded `poll()/wait(timeout)` loop, read complete atomic JSON only, validate stage/message/fraction types, and publish through `JobStatusStore` without changing cancel/failure completion behavior.

- [ ] **Step 10: Run all Task 3 tests and verify GREEN**

Run: `python -m pytest -p no:cacheprovider tests/alignment/test_quality.py tests/cli/test_evaluate_quality_cli.py tests/cli/test_run_pipeline.py tests/workflow/test_job_runner.py -q`

- [ ] **Step 11: Commit**

Commit message: `fix: report truthful quality progress`

### Task 4: Regression, main integration, and 0.1.2 rebuild

**Files:**
- Modify only if required by packaging verification: `packaging/windows/release-config.json`
- Regenerate outside Git: `dist/releases/0.1.2/CADScene-0.1.2.zip`
- Update outside Git: `dist/releases/0.1.2/CADScene-0.1.2.zip.sha256`

**Interfaces:**
- Uses the existing Windows full-runtime archive and pure-rotation backend.
- Produces a marker-free portable ZIP whose installed application version remains `0.1.2`.

- [ ] **Step 1: Run focused regression**

Run: `python -m pytest -p no:cacheprovider tests/alignment tests/cli/test_evaluate_quality_cli.py tests/cli/test_run_pipeline.py tests/workflow tests/viewer/test_suggestion_sequence.py tests/viewer/test_workflow_ui_static.py -q`

- [ ] **Step 2: Run full regression and dependency checks**

Run: `python -m pytest -p no:cacheprovider -q`

Run: `python scripts/check_no_project_dependency.py`

Run: `git diff --check`

- [ ] **Step 3: Audit exclusion**

Assert the branch diff does not add or modify `scene_bridges.py`, `scene_bridge_runner.py`, `scene_bridge_worker.py`, or related UI text.

- [ ] **Step 4: Merge to main and rerun full regression**

Use a fast-forward merge when possible, then rerun the full test suite from the main worktree.

- [ ] **Step 5: Push main**

Run: `git push origin main` only after the merged verification is green.

- [ ] **Step 6: Rebuild and verify the full bundle**

Build the 0.1.2 wheel from merged main, prepare or reuse a clean marker-free runtime archive, assemble the ZIP, run packaged `doctor`, start the packaged service, verify the suggestion module is served, and confirm the quality command contains a progress sidecar.

- [ ] **Step 7: Publish checksum and report**

Compute SHA-256, update the `.sha256` companion, and report main commit, tests, ZIP size/path/hash, actual quality milestones, and suggestion cycling behavior.
