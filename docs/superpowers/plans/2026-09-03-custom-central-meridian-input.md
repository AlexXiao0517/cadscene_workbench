# Custom Central Meridian Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept degree-minute central meridians such as `118°50′`, generate an auditable custom CGCS2000 Gauss-Kruger candidate when no EPSG matches, and use the same projection after confirmation.

**Architecture:** Keep EPSG-backed candidates unchanged and add a controlled custom-CRS branch in `cadscene.srt.georeference`. Normalize all numeric and degree-minute inputs at the API/service boundary, persist canonical decimal degrees in durable candidate jobs, and carry explicit fixed custom projection parameters through candidate confirmation and full-pose projection. Update the existing workspace dialog to parse and format the supported text forms without changing the candidate-job lifecycle.

**Tech Stack:** Python 3.11+, pyproj/PROJ, dataclasses, pytest, browser-native JavaScript and HTML.

## Global Constraints

- `118°50′` means exactly `118.833333333333…°`; never round it to `120°` or another standard EPSG central meridian.
- Custom projection uses CGCS2000/GRS80, latitude of origin `0°`, scale `1`, false easting `500000 m`, false northing `0 m`, metres, and no zone prefix.
- Existing numeric and blank/automatic input behavior remains backward compatible.
- Custom projections carry `crs_source="custom"` and `epsg=null`; no fabricated EPSG identifiers.
- Candidate scoring and confirmed full-pose projection reconstruct the CRS through one shared implementation.
- Every production behavior begins with an observed failing test.

---

### Task 1: Degree-minute parsing and controlled custom projection

**Files:**
- Modify: `cadscene/srt/georeference.py`
- Test: `tests/srt/test_georeference.py`

**Interfaces:**
- Produces: `parse_central_meridian(value: object | None) -> float | None`.
- Produces: EPSG-backed and custom `CrsCandidate` payloads with `crs_source` and nullable `epsg`.
- Produces: `CadGeoreference` reconstruction of either EPSG or fixed custom CGCS2000 projection.

- [ ] **Step 1: Write failing parser tests**

```python
@pytest.mark.parametrize("raw", ["118°50′", "118°50'", "118 50"])
def test_degree_minute_central_meridian_normalizes_to_decimal(raw: str) -> None:
    assert parse_central_meridian(raw) == pytest.approx(118.0 + 50.0 / 60.0)

@pytest.mark.parametrize("raw", ["118°60′", "181°0′", "invalid"])
def test_invalid_central_meridian_text_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="central meridian"):
        parse_central_meridian(raw)
```

- [ ] **Step 2: Run parser tests and observe RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -k "degree_minute or invalid_central" -q`

Expected: collection/import failure because `parse_central_meridian` does not exist.

- [ ] **Step 3: Implement the minimal shared parser**

Add a strict parser that accepts finite numbers, decimal strings with an optional degree suffix, and the documented degree-minute delimiters. It returns canonical decimal degrees, returns `None` for blank input, rejects minutes outside `[0, 60)`, and rejects values outside `[-180, 180]`.

- [ ] **Step 4: Run parser tests and observe GREEN**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -k "degree_minute or invalid_central" -q`

Expected: all selected tests pass.

- [ ] **Step 5: Write failing custom-candidate and confirmed-projection tests**

```python
def test_custom_118_degrees_50_minutes_candidate_uses_fixed_projection() -> None:
    meridian = parse_central_meridian("118°50′")
    candidates = recommend_cgcs2000_candidates(
        [119.071873],
        [28.896830],
        cad_bbox_raw=(500_000.0, 3_190_000.0, 550_000.0, 3_210_000.0),
        central_meridian_deg=meridian,
    )
    assert candidates[0].crs_source == "custom"
    assert candidates[0].epsg is None
    assert candidates[0].central_meridian_deg == pytest.approx(118 + 50 / 60)
    assert candidates[0].evidence["trajectory_inside_cad_ratio"] == 1.0

def test_confirmed_custom_projection_reuses_candidate_parameters() -> None:
    config = CadGeoreference.from_dict(_custom_config())
    x, y = project_wgs84_to_cad_raw(119.071873, 28.896830, config)
    assert 500_000.0 < x < 550_000.0
    assert 3_190_000.0 < y < 3_210_000.0
```

- [ ] **Step 6: Run custom projection tests and observe RED**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -k "custom_118 or confirmed_custom" -q`

Expected: custom central meridian still raises “no CGCS2000 … candidate” and custom config is rejected.

- [ ] **Step 7: Implement custom CRS candidate and reconstruction**

Add controlled custom projection constants, nullable EPSG support, `crs_source` validation, one custom CRS builder, and candidate generation for non-EPSG central meridians. Preserve the EPSG path for `117` and `120`, include the fixed parameters in serialized custom candidates/configs, and keep both CAD axis mappings.

- [ ] **Step 8: Run georeference tests and observe GREEN**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py -q`

Expected: all georeference tests pass, including unchanged EPSG behavior.

- [ ] **Step 9: Commit the domain behavior**

```bash
git add cadscene/srt/georeference.py tests/srt/test_georeference.py
git commit -m "feat: support custom central meridians"
```

### Task 2: Durable API, CLI, confirmation, and full-pose integration

**Files:**
- Modify: `cadscene/projects/http_api.py`
- Modify: `cadscene/projects/service.py`
- Modify: `cadscene/cli/build_cad_georeference_candidates.py`
- Modify: `cadscene/srt/full_pose.py`
- Test: `tests/projects/test_full_pose_configuration.py`
- Test: `tests/cli/test_build_cad_georeference_candidates_cli.py`
- Test: `tests/cli/test_build_srt_full_pose_cli.py`

**Interfaces:**
- Consumes: `parse_central_meridian` and custom `CrsCandidate`/`CadGeoreference` from Task 1.
- Produces: canonical `central_meridian_deg` in candidate requests and fingerprints.
- Produces: confirmed custom georeference configurations accepted by the full-pose adapter.

- [ ] **Step 1: Write failing API normalization and confirmation tests**

Add a project API test that POSTs `central_meridian_deg: "118°50′"`, asserts the persisted request/status contains `118.833333333333…`, runs the local executor, confirms the custom candidate, and asserts `crs_source="custom"`, `epsg is None`, and fixed parameters survive in the project snapshot.

- [ ] **Step 2: Run the API test and observe RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_full_pose_configuration.py -k "degree_minute or custom_meridian" -q`

Expected: HTTP request handling fails while converting the degree-minute string with `float()`.

- [ ] **Step 3: Implement API/service normalization and custom confirmation**

Replace independent `float()` conversion with `parse_central_meridian`, accept the nullable custom fields from the immutable candidate artifact, and pass them through `CadGeoreference.from_dict`. Ensure equivalent textual and numeric meridians produce the same request fingerprint.

- [ ] **Step 4: Run project tests and observe GREEN**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_full_pose_configuration.py -q`

Expected: all full-pose configuration tests pass.

- [ ] **Step 5: Write failing CLI and report tests**

Extend the candidate CLI fixture with `118.83333333333333` and assert output contains a custom candidate with nullable EPSG. Extend the full-pose CLI test so a custom georeference builds a trajectory and the report says “自定义 CGCS2000 高斯—克吕格” instead of `EPSG:None`.

- [ ] **Step 6: Run CLI tests and observe RED**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_build_cad_georeference_candidates_cli.py tests/cli/test_build_srt_full_pose_cli.py -q`

Expected: candidate CLI rejects the non-EPSG meridian or report renders an invalid EPSG label.

- [ ] **Step 7: Implement CLI serialization and projection-aware reporting**

Use canonical input parsing in the CLI and format the full-pose report from `crs_source`, retaining the existing EPSG label for standard candidates.

- [ ] **Step 8: Run CLI tests and observe GREEN**

Run: `python -m pytest -p no:cacheprovider tests/cli/test_build_cad_georeference_candidates_cli.py tests/cli/test_build_srt_full_pose_cli.py -q`

Expected: all selected CLI tests pass.

- [ ] **Step 9: Commit the durable workflow integration**

```bash
git add cadscene/projects/http_api.py cadscene/projects/service.py cadscene/cli/build_cad_georeference_candidates.py cadscene/srt/full_pose.py tests/projects/test_full_pose_configuration.py tests/cli/test_build_cad_georeference_candidates_cli.py tests/cli/test_build_srt_full_pose_cli.py
git commit -m "feat: persist custom CAD projections"
```

### Task 3: Workspace degree-minute input and custom-candidate presentation

**Files:**
- Modify: `apps/project_workspace/index.html`
- Modify: `apps/project_workspace/project_workspace.js`
- Test: `tests/viewer/test_project_workspace_static.py`

**Interfaces:**
- Consumes: candidate/status fields from Task 2.
- Produces: `parseCentralMeridianInput(raw)` and `formatCentralMeridian(degrees)` browser helpers.

- [ ] **Step 1: Write failing workspace contract test**

```python
def test_full_pose_dialog_accepts_degree_minute_central_meridian() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    assert 'id="centralMeridianInput" type="text"' in html
    assert "例如 120 或 118°50′；留空自动推荐" in html
    assert "function parseCentralMeridianInput" in script
    assert "function formatCentralMeridian" in script
    assert 'candidate.crs_source === "custom"' in script
    assert "自定义 CGCS2000 高斯—克吕格" in script
```

- [ ] **Step 2: Run workspace test and observe RED**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -k "degree_minute or explicit_crs" -q`

Expected: current number input and EPSG-only rendering violate the new contract.

- [ ] **Step 3: Implement browser parsing, formatting, and rendering**

Change only the central-meridian field to text/inputmode decimal. Add the strict browser parser and degree-minute formatter, submit canonical numeric degrees, restore readable degree-minute text, and render custom candidates/status/confirmation without an EPSG label. Keep progress polling and stale-input behavior unchanged.

- [ ] **Step 4: Run workspace tests and observe GREEN**

Run: `python -m pytest -p no:cacheprovider tests/viewer/test_project_workspace_static.py -q`

Expected: all workspace static tests pass.

- [ ] **Step 5: Commit the workspace behavior**

```bash
git add apps/project_workspace/index.html apps/project_workspace/project_workspace.js tests/viewer/test_project_workspace_static.py
git commit -m "feat: accept degree-minute meridians in workspace"
```

### Task 4: Documentation, full regression, real-project smoke, and service restart

**Files:**
- Modify: `docs/workflow_routing.md`
- Modify: `docs/technical/troubleshooting.md`
- Modify: `docs/design/coordinates-and-alignment.md`

**Interfaces:**
- Consumes: completed custom projection behavior from Tasks 1–3.
- Produces: operator guidance and a running local service at `http://127.0.0.1:8310/apps/project_library/`.

- [ ] **Step 1: Update operator documentation**

Document decimal/degree-minute examples, standard EPSG versus custom projection behavior, and the fixed custom parameters. Remove wording that implies every central meridian must map to an EPSG candidate.

- [ ] **Step 2: Run focused domain, API, CLI, and workspace verification**

Run: `python -m pytest -p no:cacheprovider tests/srt/test_georeference.py tests/projects/test_full_pose_configuration.py tests/cli/test_build_cad_georeference_candidates_cli.py tests/cli/test_build_srt_full_pose_cli.py tests/viewer/test_project_workspace_static.py -q`

Expected: all selected tests pass.

- [ ] **Step 3: Run the complete regression suite**

Run: `python -m pytest -p no:cacheprovider -q`

Expected: zero failures.

- [ ] **Step 4: Restart the local project-library service from the worktree**

Resolve the PID listening on port `8310`, verify its command line belongs to this project, stop that exact process, and launch the project API from this worktree with storage `D:\zjic2026\cadscene_workbench\work\srt-full-pose-ui-test`. Keep the process hidden and send stdout/stderr to the storage log directory.

- [ ] **Step 5: Verify the live endpoint and current project custom candidate**

Request `/apps/project_library/` and require HTTP 200. POST the current project candidate request using `"118°50′"`, poll the durable job to success, and verify the top candidate has `crs_source="custom"`, `epsg=null`, central meridian `118.833333333333…`, and a materially improved CAD overlap compared with the old 120° result.

- [ ] **Step 6: Commit documentation and report evidence**

```bash
git add docs/workflow_routing.md docs/technical/troubleshooting.md docs/design/coordinates-and-alignment.md
git commit -m "docs: explain custom central meridians"
```

Report the full test count, live service URL/PID, candidate job ID, top-candidate overlap evidence, and any remaining coordinate or SRT-pose limitation.
