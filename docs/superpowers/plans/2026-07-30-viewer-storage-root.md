# Viewer Storage Root Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow `serve_viewer` to serve application files from one root while storing workflow `data/` and `runs/` under another root.

**Architecture:** Keep `server.root_dir` as the static file boundary and introduce `server.storage_root_dir` as the workflow boundary. All workflow APIs and `JobRunner` use the storage root; `/data/` and `/runs/` are mounted through the existing named-root path translator when the roots differ.

**Tech Stack:** Python 3.10+, `argparse`, `http.server`, `pathlib`, pytest.

## Global Constraints

- `--storage-root` is optional; when omitted it resolves to `--root`.
- Existing directory traversal and resolved-path containment checks remain enabled.
- Dataset manifests continue to store URL paths such as `data/<dataset>/<file>`.
- No workflow routing, SfM, partial-SRT, or pure-rotation algorithm behavior changes.
- Existing `--extra-root` behavior remains compatible except that `data` and `runs` are reserved when a separate storage root is active.

---

### Task 1: Separate Static and Workflow Storage Roots

**Files:**
- Modify: `cadscene/cli/serve_viewer.py`
- Modify: `tests/cli/test_serve_viewer_upload_api.py`
- Modify: `tests/cli/test_serve_viewer_cli.py`

**Interfaces:**
- Consumes: existing `RangeRequestHandler.translate_path()`, `JobRunner(root_dir)`, workflow import functions accepting a project root.
- Produces: CLI option `--storage-root PATH`, server attribute `storage_root_dir: Path`, helper `_storage_root(server) -> Path`.

- [ ] **Step 1: Write the failing separated-root integration test**

Update the upload test helper so it can pass a separate root:

```python
def _start_server(root: Path, port: int, *, storage_root: Path | None = None) -> subprocess.Popen:
    command = [
        sys.executable,
        "-m",
        "cadscene.cli.serve_viewer",
        "--bind",
        "127.0.0.1",
        "--port",
        str(port),
        "--root",
        str(root),
    ]
    if storage_root is not None:
        command.extend(["--storage-root", str(storage_root)])
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/workflow/list-datasets")
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status in {200, 404}:
                return process
        except Exception:
            time.sleep(0.1)
    process.kill()
    raise AssertionError("server did not start")
```

Add a test that creates distinct existing `static_root` and `storage_root`, creates a dataset, uploads `flight.mp4`, and asserts:

```python
assert status == 200
assert payload["manifest"]["video"]["path"] == "data/demo/flight.mp4"
assert (storage_root / "data/demo/flight.mp4").read_bytes() == b"video-data"
assert (storage_root / "runs/demo/r1/job_status.json").is_file()
assert not (static_root / "data/demo/flight.mp4").exists()

conn = HTTPConnection("127.0.0.1", port, timeout=3)
conn.request("GET", "/data/demo/flight.mp4")
response = conn.getresponse()
assert response.status == 200
assert response.read() == b"video-data"
conn.close()
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
pytest -q tests/cli/test_serve_viewer_upload_api.py -k separate_storage_root
```

Expected: FAIL because `serve_viewer` does not recognize `--storage-root` and the server readiness loop times out.

- [ ] **Step 3: Add parser and path-root tests**

In `tests/cli/test_serve_viewer_cli.py`, assert that `build_parser()` accepts `--storage-root`, and add a focused test that a missing storage root makes `main()` return `1`.

- [ ] **Step 4: Implement the minimal storage-root split**

In `serve_viewer.py`, add:

```python
def _storage_root(server: ThreadingHTTPServer) -> Path:
    return Path(getattr(server, "storage_root_dir", getattr(server, "root_dir", project_root()))).resolve()
```

Add the parser option:

```python
parser.add_argument(
    "--storage-root",
    default=None,
    help="Store workflow data and runs under this root; defaults to --root.",
)
```

In `main()`:

```python
root = Path(args.root).resolve()
storage_root = Path(args.storage_root).resolve() if args.storage_root else root
```

Validate both roots exist. When they differ, reserve the extra-root names `data` and `runs`, then add:

```python
served_roots = dict(extra_roots)
if storage_root != root:
    served_roots["data"] = storage_root / "data"
    served_roots["runs"] = storage_root / "runs"

server.root_dir = root
server.storage_root_dir = storage_root
server.extra_roots = served_roots
server.job_runner = JobRunner(storage_root)
```

Replace workflow API uses of `self.server.root_dir` and root-based run directory construction with `_storage_root(self.server)`. Do not change `translate_path()`'s static `root_dir` boundary.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
pytest -q tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_cli.py
```

Expected: all tests pass.

- [ ] **Step 6: Run the complete test suite**

Run:

```powershell
pytest -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the implementation**

```powershell
git add cadscene/cli/serve_viewer.py tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_cli.py docs/superpowers/plans/2026-07-30-viewer-storage-root.md
git commit -m "fix: separate viewer workflow storage root"
```

### Task 2: Operational Verification

**Files:**
- No tracked source changes.

**Interfaces:**
- Consumes: `python -m cadscene.cli.serve_viewer --root <main-worktree> --storage-root <project-root>`.
- Produces: a running workflow portal whose uploads appear under the main project's `data/` and `runs/`.

- [ ] **Step 1: Stop existing viewer services**

Enumerate only Python processes whose command line contains `cadscene.cli.serve_viewer`, record their PIDs, and stop those exact PIDs.

- [ ] **Step 2: Remove obsolete worktree junctions**

Remove only `.worktrees/main-workflow/data` and `.worktrees/main-workflow/runs` after verifying both are junctions targeting the main project directories.

- [ ] **Step 3: Start the repaired service**

Run the repaired branch with:

```powershell
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8311 --root D:\zjic2026\cadscene_workbench\.worktrees\main-workflow --storage-root D:\zjic2026\cadscene_workbench
```

- [ ] **Step 4: Verify the workflow portal and storage paths**

Confirm the portal returns HTTP 200. Create disposable dataset `dataset-storage-root-smoke` and run `run-storage-root-smoke` through the local API, upload a small synthetic video payload, and confirm both:

```text
D:\zjic2026\cadscene_workbench\data\dataset-storage-root-smoke\...
D:\zjic2026\cadscene_workbench\runs\dataset-storage-root-smoke\run-storage-root-smoke\job_status.json
```

Then delete only that disposable dataset/run after resolving and verifying their paths remain under the expected `data/` and `runs/` roots.
