# Bundle-local Windows Path Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the complete Windows offline workspace inside the bundle while allowing video analysis to run from the nested 0.1.3 release directory without exceeding the 240-character native-tool path budget.

**Architecture:** Introduce compact physical names only for mutable video-analysis attempt output and immutable attempt revisions. Keep full logical revision identities in JSON, resolve compact paths first with legacy fallbacks, and make the launcher probe the exact compact layout.

**Tech Stack:** Python 3.11, `pathlib`, atomic filesystem publication, PowerShell, pytest.

## Global Constraints

- Runtime, workspace, projects, and logs remain inside the bundle.
- Do not require `LongPathsEnabled`, a virtual drive, a junction, an external user-data directory, or `\\?\` paths.
- Do not migrate or rename existing projects and attempts.
- Do not change video-analysis algorithms, API payloads, logical revisions, fingerprints, or published artifact layout.
- The legacy project ID probe must remain at or below 240 characters from the nested 0.1.3 release directory.

---

### Task 1: Compact video-analysis attempt paths with legacy reads

**Files:**
- Modify: `cadscene/video_analysis/artifacts.py`
- Modify: `cadscene/video_analysis/analyzer.py`
- Modify: `cadscene/projects/analysis_adapters.py`
- Test: `tests/video_analysis/test_publication.py`
- Test: `tests/video_analysis/test_analyzer.py`
- Test: `tests/projects/test_analysis_jobs.py`

**Interfaces:**
- Produces: `VIDEO_ANALYSIS_DIRECTORY = "v"` and `ANALYSIS_REVISIONS_DIRECTORY = "r"` in `cadscene.video_analysis.artifacts`.
- Consumes: `publish_analysis_revision(output_dir: Path, revision: str, payloads: Mapping[str, str]) -> Path`.
- Preserves: `_video_analysis_output_dir(job: QueueJob, revision: str) -> Path` resolves both compact and legacy output trees.

- [ ] **Step 1: Write failing compact-layout tests**

Add assertions that a new analyzer publication is rooted at `attempt/v/r/<bounded-revision>`, its pointer contains `r/<bounded-revision>`, and neither new path contains `02_video_analysis` nor `analysis_revisions`:

```python
assert published.parent == output / "r"
assert published.parent.parent.name == "v"
pointer = json.loads((output / "current_analysis_revision.json").read_text(encoding="utf-8"))
assert pointer["revision_directory"] == f"r/{published.name}"
```

Add an adapter regression that constructs the prior `attempt/02_video_analysis/analysis_revisions/<revision>/clip_manifest.json` tree and confirms `validate_video_outputs()` still succeeds.

- [ ] **Step 2: Run the focused tests and verify the old layout fails the new expectations**

Run:

```powershell
python -m pytest -p no:cacheprovider tests/video_analysis/test_publication.py tests/video_analysis/test_analyzer.py tests/projects/test_analysis_jobs.py -q
```

Expected: failures show new publications still use `02_video_analysis/analysis_revisions` or the compact constants are missing.

- [ ] **Step 3: Implement the compact writer and compatibility resolver**

Define the physical-name constants and use them when creating new revisions:

```python
VIDEO_ANALYSIS_DIRECTORY = "v"
ANALYSIS_REVISIONS_DIRECTORY = "r"

revisions_dir = output_dir / ANALYSIS_REVISIONS_DIRECTORY
revision_directory = f"{ANALYSIS_REVISIONS_DIRECTORY}/{destination.name}"
```

Make the analyzer call:

```python
published = publish_analysis_revision(
    Path(output_root) / VIDEO_ANALYSIS_DIRECTORY,
    revision,
    payloads,
)
```

Resolve roots in this order in `analysis_adapters.py`:

```python
roots = (
    attempt / VIDEO_ANALYSIS_DIRECTORY,
    attempt / "02_video_analysis",
)
revision_dirs = (
    root / ANALYSIS_REVISIONS_DIRECTORY / revision,
    root / "analysis_revisions" / revision,
    root / revision,
)
```

For every root, retain current-pointer validation and containment checks. Return the compact indexed candidate when no output exists so a missing-output diagnostic points at the new layout.

- [ ] **Step 4: Run focused tests**

Run the Task 1 command again.

Expected: all selected tests pass and legacy resolution remains covered.

- [ ] **Step 5: Commit the compact attempt layout**

```powershell
git add cadscene/video_analysis/artifacts.py cadscene/video_analysis/analyzer.py cadscene/projects/analysis_adapters.py tests/video_analysis/test_publication.py tests/video_analysis/test_analyzer.py tests/projects/test_analysis_jobs.py
git commit -m "fix: compact video analysis attempt paths"
```

### Task 2: Align the offline launcher with the compact path budget

**Files:**
- Modify: `packaging/windows/launcher/start.ps1`
- Modify: `tests/packaging/test_windows_offline_bundle.py`

**Interfaces:**
- Consumes: the Task 1 physical layout `v/r/<bounded-revision>`.
- Preserves: `Assert-PathBudget()` and `$maxSafePathLength = 240`.
- Produces: a probe that covers a legacy `dataset-UUID` while passing under the actual nested 0.1.3 release root.

- [ ] **Step 1: Write the failing launcher regression**

Require the script to probe:

```text
v\r\r-0000000000000000\video_analysis_manifest.json
```

Construct the actual nested workspace in the Python test:

```python
nested_workspace = Path(
    r"D:\zjic2026\cadscene_workbench\dist\releases\0.1.3\CADScene-0.1.3\workspace"
)
probe = nested_workspace / (
    "projects/dataset-00000000-0000-0000-0000-000000000000/"
    "jobs/00000000000000000000000000000000/attempt-1/"
    "v/r/r-0000000000000000/video_analysis_manifest.json"
)
assert len(str(probe)) <= 240
```

Also assert the script no longer probes `02_video_analysis\analysis_revisions`.

- [ ] **Step 2: Run the packaging test and verify failure**

Run:

```powershell
python -m pytest -p no:cacheprovider tests/packaging/test_windows_offline_bundle.py -q
```

Expected: the source assertion fails because the launcher still models the verbose attempt tree.

- [ ] **Step 3: Update only the probe path**

Replace the verbose probe entry with:

```powershell
(Join-Path $probeJobRoot "v\r\r-0000000000000000\video_analysis_manifest.json")
```

Keep the published-artifact probes and the 240-character rejection guard unchanged.

- [ ] **Step 4: Run packaging and PowerShell parser tests**

Run:

```powershell
python -m pytest -p no:cacheprovider tests/packaging/test_windows_offline_bundle.py -q
```

Expected: all tests pass, including parsing both launcher scripts.

- [ ] **Step 5: Commit the launcher fix**

```powershell
git add packaging/windows/launcher/start.ps1 tests/packaging/test_windows_offline_bundle.py
git commit -m "fix: preserve bundle-local workspace on nested paths"
```

### Task 3: Regression verification and 0.1.3 package replacement

**Files:**
- Generated: `dist/releases/0.1.3-longpath-fix/CADScene-0.1.3/`
- Generated: `dist/releases/0.1.3-longpath-fix/CADScene-0.1.3.zip`
- Generated: `dist/releases/0.1.3-longpath-fix/CADScene-0.1.3.zip.sha256`

**Interfaces:**
- Consumes: compact attempt layout and corrected launcher template.
- Produces: a verified 0.1.3 offline bundle that starts from the nested release directory and writes to its own `workspace`.

- [ ] **Step 1: Run focused regression suites**

```powershell
python -m pytest -p no:cacheprovider tests/video_analysis tests/projects/test_analysis_jobs.py tests/packaging/test_windows_offline_bundle.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full repository verification**

```powershell
python -m pytest -p no:cacheprovider -q
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: pytest has zero failures, dependency check exits 0, and `git diff --check` prints nothing.

- [ ] **Step 3: Build a fresh wheel and packed runtime without touching the existing extracted package**

```powershell
python -m build --wheel --outdir dist/releases/0.1.3-longpath-fix/wheels
python scripts/windows_offline_bundle.py prepare-runtime --conda D:/anaconda3/Scripts/conda.exe --conda-pack D:/anaconda3/Scripts/conda-pack.exe --source-prefix .worktrees/fix-windows-path-and-scene-segmentation/.local/compact-paths-9858176/build-env --build-prefix .local/release-0.1.3-longpath/build-env --wheel dist/releases/0.1.3-longpath-fix/wheels/cadscene_workbench-0.1.3-py3-none-any.whl --runtime-archive .local/release-0.1.3-longpath/runtime.tar.gz
$cadsceneSourceCommit = git rev-parse HEAD
python scripts/windows_offline_bundle.py assemble --config packaging/windows/release-config.json --runtime-archive .local/release-0.1.3-longpath/runtime.tar.gz --backend-root dist/releases/0.1.3/CADScene-0.1.3/pure_rotation_backend --templates-root packaging/windows --output-dir dist/releases/0.1.3-longpath-fix --source-commit $cadsceneSourceCommit --zip
python scripts/windows_offline_bundle.py verify --config packaging/windows/release-config.json --bundle-root dist/releases/0.1.3-longpath-fix/CADScene-0.1.3
```

Expected: the new output is generated under `0.1.3-longpath-fix`; the existing `dist/releases/0.1.3/CADScene-0.1.3` directory is not removed or overwritten.

- [ ] **Step 4: Verify the rebuilt bundle in place without starting a persistent service**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File dist/releases/0.1.3-longpath-fix/CADScene-0.1.3/launcher/start.ps1 -PreferredPort 8300 -NoBrowser
$cadsceneState = Get-Content -Raw -LiteralPath dist/releases/0.1.3-longpath-fix/CADScene-0.1.3/launcher/service-state.json | ConvertFrom-Json
$cadsceneExpectedWorkspace = [IO.Path]::GetFullPath('dist/releases/0.1.3-longpath-fix/CADScene-0.1.3/workspace')
if ([IO.Path]::GetFullPath([string]$cadsceneState.storage_root) -ine $cadsceneExpectedWorkspace) { throw 'packaged service left the bundle-local workspace' }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File dist/releases/0.1.3-longpath-fix/CADScene-0.1.3/launcher/stop.ps1
if (Get-NetTCPConnection -LocalPort ([int]$cadsceneState.port) -State Listen -ErrorAction SilentlyContinue) { throw 'packaged service is still listening' }
```

Expected: startup succeeds from the nested release path, `storage_root` equals the new bundle's own `workspace`, and the stop script removes the listener.

- [ ] **Step 5: Record final repository and package state**

```powershell
git status --short
git log -3 --oneline
$cadsceneZip = 'dist/releases/0.1.3-longpath-fix/CADScene-0.1.3.zip'
$cadsceneHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $cadsceneZip).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$cadsceneZip.sha256" -Value "$cadsceneHash  CADScene-0.1.3.zip" -Encoding ascii
Get-Content -LiteralPath "$cadsceneZip.sha256"
```

Expected: source worktree is clean after commits; checksum matches `CADScene-0.1.3.zip.sha256`.
