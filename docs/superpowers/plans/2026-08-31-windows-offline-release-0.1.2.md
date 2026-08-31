# Windows Offline Release 0.1.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a tested `CADScene-0.1.2` complete Windows offline bundle with approved bug fixes and without adjacent-clip up/down bridging.

**Architecture:** Integrate an explicit commit allowlist onto the Windows release baseline, then version and build a fresh wheel into a dedicated packed runtime. Validate SfM intrinsics from algorithm artifact through API, browser contract, wheel, installed bundle, and real HTTP smoke.

**Tech Stack:** Python 3.10, pytest, setuptools/wheel, conda-pack, PowerShell 5.1, ZIP64, existing `cadscene-workbench` CLI.

## Global Constraints

- Release version and bundle directory are exactly `0.1.2` and `CADScene-0.1.2`.
- Do not include adjacent-clip locating, precomputed scene overlap, up/down scene bridging, or scene-bridge publication.
- Do not merge or modify `main`.
- Preserve the pinned Pure Rotation and OpenGV backend versions.
- Do not delete the existing extracted package while the user is testing it.

---

### Task 1: Integrate the approved patch allowlist

**Files:**
- Modify: files changed by the selected commits under `cadscene/projects/`, `apps/web_camera_viewer/`, `apps/project_workspace/`, and their tests.
- Test: `tests/projects/test_workbench_sessions.py`
- Test: `tests/projects/test_service_jobs.py`
- Test: `tests/viewer/test_workflow_ui_static.py`

**Interfaces:**
- Consumes: Windows release baseline at `9858176`.
- Produces: approved progress, FOV, cancellation, and workbench-resume behavior without scene bridging.

- [ ] **Step 1: Record the release baseline diff**

Run `git diff --name-only 9858176...HEAD` and save the expected empty starting result.

- [ ] **Step 2: Cherry-pick the approved commits in dependency order**

Apply `4707e09 483b83f 1a304f1 9289c23 e262206 1c4c564 0e034cc 30c3039 b200596 934bedc 19e1e7a` and resolve only direct context conflicts.

- [ ] **Step 3: Verify the exclusion contract**

Run `git diff --name-only 9858176...HEAD` and assert it contains none of `scene_bridges.py`, `scene_bridge_runner.py`, `scene_bridge_worker.py`, or scene-bridge UI files.

- [ ] **Step 4: Run focused tests**

Run `python -m pytest -p no:cacheprovider tests/projects/test_workbench_sessions.py tests/projects/test_service_jobs.py tests/viewer/test_workflow_ui_static.py tests/viewer/test_annotation_pts.py tests/viewer/test_keyframe_sources.py -q` and expect all tests to pass.

### Task 2: Set and test release version 0.1.2

**Files:**
- Modify: `pyproject.toml`
- Modify: `packaging/windows/release-config.json`
- Modify: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Consumes: `ReleaseConfig.load(path)`.
- Produces: application version `0.1.2` and bundle name `CADScene-0.1.2`.

- [ ] **Step 1: Write the failing version assertions**

Change the release-config test to expect `CADScene-0.1.2` and `0.1.2`; add an assertion that `pyproject.toml` declares `version = "0.1.2"`.

- [ ] **Step 2: Verify RED**

Run `python -m pytest -p no:cacheprovider tests/packaging/test_windows_offline_bundle.py::test_release_config_pins_bundle_and_backend_versions tests/packaging/test_project_metadata.py -q` and expect a version assertion failure.

- [ ] **Step 3: Implement the version update**

Set `pyproject.toml` to `0.1.2` and `packaging/windows/release-config.json` to bundle `CADScene-0.1.2`, application version `0.1.2`.

- [ ] **Step 4: Verify GREEN**

Repeat the focused command and expect all tests to pass.

- [ ] **Step 5: Commit**

Commit the version and test changes as `build: prepare Windows offline release 0.1.2`.

### Task 3: Verify the SfM FOV data path

**Files:**
- Test: `tests/sfm/test_camera_init.py`
- Test: `tests/viewer/test_workflow_ui_static.py`
- Test: `tests/cli/test_serve_viewer_workflow_api.py`

**Interfaces:**
- Consumes: `load_sfm_camera_initialization(path)` and `/api/workflow/sfm-camera-init`.
- Produces: non-default horizontal FOV derived from SfM `width` and `fx`, applied only over placeholder camera state.

- [ ] **Step 1: Run the existing non-70-degree FOV tests**

Run `python -m pytest -p no:cacheprovider tests/sfm/test_camera_init.py tests/cli/test_serve_viewer_workflow_api.py tests/viewer/test_workflow_ui_static.py -q` and expect all tests to pass.

- [ ] **Step 2: Audit the complete data flow**

Confirm the reconstruction artifact contains intrinsics, the endpoint reads `02_sfm/camera_trajectory.json`, and the browser waits for viewer readiness before applying `safe_fields=["fov"]`.

- [ ] **Step 3: Verify placeholder versus authoritative-track behavior**

Run the tests named `test_sfm_fov_preserves_authoritative_track_but_repairs_default_placeholder` and `test_sfm_fov_initialization_is_page_local_so_refresh_reapplies_intrinsics`; expect both to pass.

### Task 4: Build and inspect the wheel

**Files:**
- Generated: `dist/cadscene_workbench-0.1.2-py3-none-any.whl`

**Interfaces:**
- Consumes: the release branch source tree.
- Produces: installable wheel with Python modules, configs, and web assets.

- [ ] **Step 1: Build a fresh wheel**

Run `python -m build --wheel --outdir dist` and expect the 0.1.2 wheel.

- [ ] **Step 2: Inspect wheel contents**

Verify it contains `cadscene/projects/workbench_resume.py`, `apps/web_camera_viewer/workflow.js`, `viewer_legacy.js`, `annotation_pts.js`, and package metadata version `0.1.2`.

- [ ] **Step 3: Run packaging tests**

Run `python -m pytest -p no:cacheprovider tests/packaging -q` and expect all tests to pass.

### Task 5: Assemble the complete offline bundle

**Files:**
- Generated: `dist/CADScene-0.1.2/`
- Generated: `dist/CADScene-0.1.2.zip`
- Generated: `dist/CADScene-0.1.2.zip.sha256`

**Interfaces:**
- Consumes: 0.1.2 wheel, pinned Pure Rotation backend, build environment, and Windows launcher templates.
- Produces: complete portable bundle with isolated runtime and writable `workspace/`.

- [ ] **Step 1: Create a dedicated runtime**

Use `scripts/windows_offline_bundle.py prepare-runtime` with the current validated conda environment and the 0.1.2 wheel; require `pip check` and dependency import smoke to succeed.

- [ ] **Step 2: Assemble and verify**

Use `scripts/windows_offline_bundle.py assemble --zip` with the pinned backend and current release commit, then run the `verify` subcommand.

- [ ] **Step 3: Write SHA-256**

Hash `CADScene-0.1.2.zip` and write the standard `<hash>  CADScene-0.1.2.zip` checksum file.

### Task 6: Run packaged runtime and FOV smoke tests

**Files:**
- Inspect: `dist/CADScene-0.1.2/launcher/release.json`
- Inspect: installed files below `dist/CADScene-0.1.2/runtime/Lib/site-packages/`

**Interfaces:**
- Consumes: assembled offline bundle.
- Produces: evidence that the shipped runtime serves the approved code and SfM FOV.

- [ ] **Step 1: Run packaged doctor**

Run the bundled Python executable with `-m cadscene.cli.main doctor --storage-root <bundle workspace>` and expect required dependencies and tools to pass.

- [ ] **Step 2: Start the packaged server on a free port**

Start `cadscene.cli.serve_viewer` from the bundle and wait for `/api/projects` to return HTTP 200.

- [ ] **Step 3: Verify a real non-70 FOV response**

Copy or reference an existing read-only run with non-70 SfM intrinsics, call `/api/workflow/sfm-camera-init`, and assert the returned FOV matches the artifact calculation rather than 70.

- [ ] **Step 4: Verify installed browser contract**

Confirm the served `viewer_legacy.js` distinguishes default placeholder tracks from authoritative tracks and the served `workflow.js` contains the resume and FOV initialization paths.

- [ ] **Step 5: Stop the packaged server**

Terminate only the process started by this smoke test.

### Task 7: Final regression and release audit

**Files:**
- Inspect: entire release branch and generated manifest.

**Interfaces:**
- Consumes: completed release branch and bundle.
- Produces: release readiness report.

- [ ] **Step 1: Run the full regression suite**

Run `python -m pytest -p no:cacheprovider -q` and require zero failures.

- [ ] **Step 2: Run repository checks**

Run `python scripts/check_no_project_dependency.py` and `git diff --check`; require both to succeed.

- [ ] **Step 3: Audit exclusions and metadata**

Confirm no scene-bridge commits/files entered the release diff, `release.json` references the release commit, and the bundle reports version 0.1.2.

- [ ] **Step 4: Report artifacts**

Report the release worktree, branch, commit allowlist, excluded feature, test counts, FOV smoke value, ZIP path, size, and SHA-256 without merging main.
