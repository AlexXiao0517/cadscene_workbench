# Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the current source checkout and built wheel expose the same supported applications, configurations, CLI, dependencies, and environment diagnostics without adding a license or desktop installer.

**Architecture:** Keep `apps/` and `configs/` as the single source of truth, include them as wheel package data, and resolve the application root through one Python helper. Add a thin console dispatcher over the existing server and a side-effect-free doctor model whose probes are injectable in tests.

**Tech Stack:** Python 3.10+, setuptools, argparse, importlib, pathlib, pytest.

## Global Constraints

- Do not add an open-source license.
- `sfm_only` and `pure_rotation` are supported workflows; only automatic video analysis/recommendation remains immature.
- `pycolmap` is a default dependency.
- Do not install Torch/Transformers or retain the semantic `segmentation` extra.
- Keep `pure_rotation_camera_poc` external and pinned; do not move or modify it.
- Do not touch local projects, `dji`, `data`, `runs`, or `work`.

---

### Task 1: Repository cleanup and dependency contract

**Files:**
- Delete: `apps/web_camera_viewer_broken_stage3f/**`
- Modify: `pyproject.toml`
- Create: `tests/packaging/test_project_metadata.py`

**Interfaces:**
- Consumes: PEP 621 project metadata.
- Produces: default runtime dependencies and `cadscene-workbench` console script metadata used by later wheel tests.

- [ ] **Step 1: Write the failing metadata test**

```python
def test_default_install_declares_supported_runtime_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {item.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].lower() for item in project["dependencies"]}
    assert {"numpy", "scipy", "pyyaml", "pillow", "opencv-python", "ezdxf", "imageio-ffmpeg", "pycolmap"} <= names
    assert "segmentation" not in project.get("optional-dependencies", {})
    assert project["scripts"]["cadscene-workbench"] == "cadscene.cli.main:main"
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest -p no:cacheprovider tests/packaging/test_project_metadata.py -q`

Expected: FAIL because dependencies and the console script are missing.

- [ ] **Step 3: Update metadata and remove only the broken viewer copy**

Add the eight supported runtime dependencies, retain diagnostics/dev extras, remove the semantic segmentation extra, add the console script, and remove `apps/web_camera_viewer_broken_stage3f` without touching the official viewer.

- [ ] **Step 4: Run the metadata test and existing static viewer tests**

Run: `python -m pytest -p no:cacheprovider tests/packaging/test_project_metadata.py tests/viewer -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add pyproject.toml tests/packaging apps/web_camera_viewer_broken_stage3f
git commit -m "build: declare supported runtime dependencies"
```

### Task 2: Wheel application resources

**Files:**
- Create: `apps/__init__.py`
- Create: `configs/__init__.py`
- Create: `cadscene/application_resources.py`
- Modify: `pyproject.toml`
- Modify: `cadscene/cli/serve_viewer.py`
- Create: `tests/packaging/test_application_resources.py`
- Create: `tests/packaging/test_wheel_contents.py`

**Interfaces:**
- Produces: `application_root() -> Path`, `apps_root() -> Path`, `configs_root() -> Path`, and wheel members below `apps/` and `configs/`.
- Consumes: existing `serve_viewer.project_root()` callers; its public behavior remains compatible.

- [ ] **Step 1: Write failing source/resource tests**

```python
def test_application_resources_find_official_apps_and_configs() -> None:
    root = application_root()
    assert (root / "apps/workflow_portal/index.html").is_file()
    assert (root / "apps/project_workspace/index.html").is_file()
    assert (root / "apps/web_camera_viewer/index.html").is_file()
    assert (root / "configs/pipelines/sfm_overlay_existing_sfm.yaml").is_file()
    assert not (root / "apps/web_camera_viewer_broken_stage3f").exists()
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -p no:cacheprovider tests/packaging/test_application_resources.py -q`

Expected: FAIL because the locator does not exist.

- [ ] **Step 3: Implement the minimal locator and package data**

`application_root()` resolves `Path(__file__).parents[1]`, validates both `apps/` and `configs/`, and raises a user-readable `FileNotFoundError` otherwise. Configure setuptools to include recursively tracked HTML, JS, CSS, PNG, SVG, Markdown, YAML and TXT data from the two marker packages. Make `serve_viewer.project_root()` delegate to this helper.

- [ ] **Step 4: Add a wheel ZIP contract test and verify GREEN**

The test builds a wheel into `tmp_path`, opens it with `zipfile.ZipFile`, and asserts official pages/configs exist while the broken viewer does not. Run:

`python -m pytest -p no:cacheprovider tests/packaging/test_application_resources.py tests/packaging/test_wheel_contents.py tests/cli/test_serve_viewer_cli.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```text
git add apps/__init__.py configs/__init__.py cadscene/application_resources.py cadscene/cli/serve_viewer.py pyproject.toml tests/packaging
git commit -m "build: package viewer applications and configs"
```

### Task 3: Unified CLI and doctor

**Files:**
- Create: `cadscene/cli/main.py`
- Create: `cadscene/cli/doctor.py`
- Create: `tests/cli/test_main_cli.py`
- Create: `tests/cli/test_doctor.py`

**Interfaces:**
- Produces: `cadscene.cli.main.main(argv: list[str] | None = None) -> int`.
- Produces: `run_doctor(options: DoctorOptions, probes: DoctorProbes | None = None) -> DoctorReport`.
- Preserves: `python -m cadscene.cli.serve_viewer`.

- [ ] **Step 1: Write failing dispatcher tests**

```python
def test_cli_dispatches_serve_without_reparsing_server_options(monkeypatch):
    monkeypatch.setattr(serve_viewer, "main", lambda argv: 23 if argv == ["--port", "9001"] else 99)
    assert main(["serve", "--port", "9001"]) == 23

def test_cli_dispatches_doctor(monkeypatch):
    monkeypatch.setattr(doctor, "main", lambda argv: 17 if argv == ["--json"] else 99)
    assert main(["doctor", "--json"]) == 17
```

- [ ] **Step 2: Verify RED and implement the thin dispatcher**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_main_cli.py -q`

Expected before implementation: FAIL; after the minimal dispatcher: PASS.

- [ ] **Step 3: Write failing doctor model tests**

Cover healthy resources, missing pycolmap, missing FFmpeg, missing external OpenGV backend, an invalid pinned backend, and an unwritable storage root. Assert each check has `name`, `status`, `required`, and `message`; missing OpenGV makes `complete_capability=False` but does not claim Pure Rotation is experimental.

- [ ] **Step 4: Implement doctor probes and renderers**

Use `importlib.util.find_spec`, `shutil.which`, `ExternalOpenGVBackend.health_check`, existing Pure Rotation runtime discovery, and a create/fsync/delete write probe in the selected storage root. Human output groups core, SfM, Pure Rotation and storage; `--json` emits the same typed report.

- [ ] **Step 5: Run focused CLI tests**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_main_cli.py tests/cli/test_doctor.py tests/cli/test_serve_viewer_cli.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```text
git add cadscene/cli/main.py cadscene/cli/doctor.py tests/cli pyproject.toml
git commit -m "feat: add supported environment doctor"
```

### Task 4: Documentation and release verification

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/technical/developer-guide.md`
- Modify: `CHANGELOG.md`
- Test: all suites

**Interfaces:**
- Produces: accurate install/start/doctor instructions and capability labels.

- [ ] **Step 1: Add failing documentation contract assertions**

Extend packaging/static tests to require `cadscene-workbench doctor`, `cadscene-workbench serve`, supported SfM/Pure Rotation wording, and absence of the old segmentation install extra and Pure Rotation Experimental label.

- [ ] **Step 2: Verify RED, update docs, and verify GREEN**

Run focused documentation/packaging tests before and after the edits.

- [ ] **Step 3: Build and inspect a real wheel**

Run: `python -m build --wheel`

Install the wheel with `pip --target` into a temporary directory and run `cadscene.cli.main` through that target to verify resource discovery and `doctor --json` without importing source-tree modules.

- [ ] **Step 4: Run full verification**

```text
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: all tests pass; dependency scan and diff check succeed.

- [ ] **Step 5: Commit**

```text
git add README.md README_EN.md docs/technical/developer-guide.md CHANGELOG.md tests
git commit -m "docs: document supported local installation"
```
