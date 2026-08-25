# Windows Offline Tester Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained Windows x64 ZIP that non-programmer testers can start and stop by double-clicking without installing or downloading dependencies.

**Architecture:** A tested Python release builder assembles a whitelisted bundle from a dedicated Conda environment and a pinned Pure Rotation backend. Thin CMD entry points call a PowerShell launcher that relocates the packed environment once, validates it with `doctor`, starts one localhost service, persists process identity, and opens the project library.

**Tech Stack:** Python 3.10, pytest, setuptools wheel, conda-pack, PowerShell 5.1, ZIP64, existing `cadscene-workbench` CLI.

## Global Constraints

- Do not merge commits at or after `6e094aa` from `codex/async-job-progress-scene-positioning`.
- Include no existing projects, `dji` source data, `data`, `runs`, Git metadata, tests, logs, or debug outputs.
- Require no preinstalled Python, Conda, FFmpeg, or first-run network access.
- Bind only to `127.0.0.1`; persist all user data below bundle-local `workspace`.
- Preserve current MP4/DXF/SRT, SfM, Pure Rotation, workbench, annotation, render, and concat behavior.

---

### Task 1: Release layout and exclusion policy

**Files:**
- Create: `scripts/windows_offline_bundle.py`
- Test: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Produces: `BundleLayout`, `BackendCopyPlan`, `validate_staging_tree(root)` and `write_zip64(source, output)`.

- [ ] Write tests that reject `.git`, `projects`, `data`, `runs`, test directories, logs and non-whitelisted Pure Rotation outputs.
- [ ] Run `python -m pytest -p no:cacheprovider tests/packaging/test_windows_offline_bundle.py -q` and confirm the missing module failure.
- [ ] Implement the minimal layout, backend allowlist, tree validator and deterministic ZIP64 writer.
- [ ] Run the focused tests and confirm they pass.
- [ ] Commit as `feat: define offline tester bundle layout`.

### Task 2: Safe one-click launcher

**Files:**
- Create: `packaging/windows/启动CAD视频工作台.cmd`
- Create: `packaging/windows/关闭CAD视频工作台.cmd`
- Create: `packaging/windows/launcher/start.ps1`
- Create: `packaging/windows/launcher/stop.ps1`
- Create: `packaging/windows/使用说明.txt`
- Modify: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Consumes: bundle-relative `runtime`, `pure_rotation_backend`, `workspace`, `logs`.
- Produces: `launcher/service-state.json`, HTTP-ready browser launch, identity-checked stop.

- [ ] Add static and executable-contract tests for relative paths, localhost binding, doctor arguments, project-library URL, relocation marker and strict PID identity.
- [ ] Run the focused tests and confirm they fail because templates are absent.
- [ ] Implement the two CMD shims and PowerShell start/stop scripts with user-readable Chinese failures.
- [ ] Run the focused tests and PowerShell parser checks.
- [ ] Commit as `feat: add portable Windows launcher`.

### Task 3: Reproducible environment and bundle builder

**Files:**
- Modify: `scripts/windows_offline_bundle.py`
- Create: `packaging/windows/release-config.json`
- Modify: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Produces CLI commands `prepare-runtime`, `assemble`, and `verify`.
- Uses a dedicated build-prefix, current wheel, conda-pack and pinned backend version metadata.

- [ ] Add command-construction and fake-runtime assembly tests.
- [ ] Verify the tests fail on missing builder commands.
- [ ] Implement wheel build/install, `pip check`, conda-pack extraction, backend copy, manifest hashing and ZIP creation.
- [ ] Verify focused tests pass and `--help` documents all required inputs.
- [ ] Commit as `build: create Windows offline tester bundle`.

### Task 4: Build and acceptance verification

**Files:**
- Modify only if a test exposes a packaging defect.

**Interfaces:**
- Produces: `dist/CADSceneWorkbench-0.1.0-win64-offline.zip` and SHA-256 report.

- [ ] Create a dedicated build environment from the known Pure Rotation environment and install the current wheel dependencies, including pycolmap.
- [ ] Run `pip check`, `cadscene-workbench doctor`, and the Pure Rotation Python import probe inside that environment.
- [ ] Build the ZIP and verify its exclusion manifest.
- [ ] Extract to a temporary path containing spaces and Chinese characters.
- [ ] Run offline doctor, start the service, request `/apps/project_library/`, stop it, and repeat once.
- [ ] Run `python -m pytest -p no:cacheprovider`, `python scripts/check_no_project_dependency.py`, `git diff --check`, and confirm the worktree is clean after committing release tooling.

