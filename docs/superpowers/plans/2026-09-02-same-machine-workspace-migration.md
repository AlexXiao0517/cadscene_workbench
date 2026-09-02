# Same-machine Workspace Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve old CADScene project progress when an entire workspace is copied from an older bundle to a newer bundle on the same Windows computer.

**Architecture:** A Python module performs deterministic discovery and copy validation without mutating manifests. The PowerShell launcher consumes its JSON plan, checks for live old services, performs a recoverable directory rename plus Junction creation, and asks the Python module to persist or validate the migration marker.

**Tech Stack:** Python 3.10+, pathlib/json/argparse, PowerShell 5.1 Junctions, pytest.

## Global Constraints

- Same Windows machine only.
- Do not rewrite manifests, revisions, fingerprints, databases, media, or algorithm outputs.
- Never delete the old workspace; preserve it as a timestamped sibling backup.
- Stop startup on ambiguous roots, incomplete copies, a live old service, or a missing/wrong Junction.
- Use TDD and keep the existing bundle-local runtime/storage behavior.

---

### Task 1: Migration discovery and validation

**Files:**
- Create: `cadscene/cli/workspace_migration.py`
- Create: `tests/cli/test_workspace_migration.py`

**Interfaces:**
- Produces: `build_migration_plan(workspace: Path) -> MigrationPlan`
- Produces CLI commands `plan --workspace PATH` and `record --workspace PATH --old-workspace PATH --backup-workspace PATH`.

- [ ] **Step 1: Write failing tests** for no-op workspaces, one copied old root, multiple old roots, missing copied references, and a persisted marker whose Junction target is injected for platform-independent testing.
- [ ] **Step 2: Run** `python -m pytest -p no:cacheprovider tests/cli/test_workspace_migration.py -q` and confirm failures are caused by the missing module/API.
- [ ] **Step 3: Implement minimal discovery** by recursively reading JSON string values, extracting absolute Windows paths above `projects`, `data`, or `runs`, comparing old manifest inventories, and validating rebased existing references.
- [ ] **Step 4: Implement marker recording/validation** with atomic JSON replacement and stable JSON CLI output; all user-facing failures use actionable Chinese messages.
- [ ] **Step 5: Re-run** the focused test file and confirm it passes.

### Task 2: Recoverable launcher integration

**Files:**
- Modify: `packaging/windows/launcher/start.ps1`
- Modify: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Consumes: `python -m cadscene.cli.workspace_migration plan --workspace $workspace`.
- Produces: `Invoke-WorkspaceMigration`, which returns only after no migration is needed or a verified Junction exists.

- [ ] **Step 1: Add failing launcher contract tests** asserting migration runs before doctor, live old-workspace processes are rejected, old workspace uses `Move-Item -LiteralPath`, Junction creation uses `New-Item -ItemType Junction`, failure rolls the backup back, and `record` is called only after success.
- [ ] **Step 2: Run** `python -m pytest -p no:cacheprovider tests/packaging/test_windows_offline_bundle.py -q` and confirm the new assertions fail.
- [ ] **Step 3: Implement the minimal PowerShell orchestration** with exact-path validation, CIM command-line checks, recoverable rename/Junction creation, rollback, and Chinese errors.
- [ ] **Step 4: Run focused Python and packaging tests**, including the PowerShell parser test, until green.

### Task 3: Bundle synchronization and regression verification

**Files:**
- Modify after commit: `dist/releases/0.1.3-fix/CADScene-0.1.3/launcher/start.ps1`
- Modify after commit: bundled `cadscene/cli/workspace_migration.py` and `launcher/release.json`.

**Interfaces:**
- Bundle launcher and installed module must byte-match the committed source files.

- [ ] **Step 1: Run** `python -m pytest -p no:cacheprovider tests/cli/test_workspace_migration.py tests/packaging/test_windows_offline_bundle.py -q`.
- [ ] **Step 2: Run** `python -m pytest -p no:cacheprovider` plus `python scripts/check_no_project_dependency.py` and `git diff --check`.
- [ ] **Step 3: Commit source changes** with one focused commit.
- [ ] **Step 4: Copy the committed launcher/module into the 0.1.3-fix bundle**, update `source_commit`, and compare SHA-256 hashes.
- [ ] **Step 5: Do not restart the running tester service**; report the migration contract, tests, bundle synchronization, and remaining manual acceptance step.

