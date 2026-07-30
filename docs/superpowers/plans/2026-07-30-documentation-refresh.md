# CADScene Workbench Documentation Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the outdated documentation with a bilingual, designer-oriented project guide and accurate design, technical, workflow, and change documentation.

**Architecture:** `README.md` and `README_EN.md` are concise product-use entrances for architecture and road-design professionals. `docs/README.md` routes readers to task-oriented user guidance, design rationale, and detailed engineering references; technical facts and status labels have one consistent meaning across every document.

**Tech Stack:** Markdown, repository-relative links, Python/pytest verification, Git.

## Global Constraints

- The primary README audience is architecture and road-design professionals without a computer-science background.
- README content follows the user journey: prepare inputs, select a workflow, follow progress, calibrate keyframes, fit the route, inspect and export results.
- README does not contain a module inventory, complete CLI catalogue, test instructions, or internal API detail; those belong under `docs/technical/`.
- Chinese `README.md` and English `README_EN.md` have matching structure, commands, capability status, and limitations.
- Use these status labels consistently: `sfm_only` Stable; `pure_rotation` Experimental; partial-SRT core Experimental CLI; `srt_sfm_fused` portal route Interface only; `srt_full_pose` Interface only; SfM CUDA Optional.
- Do not document rank1, GPU bundle adjustment, automatic motion classification, or other features absent from `main`.
- State that default SfM is `pycolmap + cpu`; optional CUDA only accelerates supported feature extraction and matching and can fall back to CPU.
- State that consistent confirmed manual keyframe FOV values override unreliable reconstructed FOV and that ordinary SRT is not precision pose truth.
- State that pure rotation fixes the camera center and does not recover translation or scale.
- Do not add or imply CI, coverage, release, or license status that the repository does not have.
- Do not change runtime code, algorithms, APIs, or frontend behavior.

---

### Task 1: Designer-oriented bilingual project entrance

**Files:**
- Modify: `README.md`
- Create: `README_EN.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: Current UI labels, workflow status, runtime boundaries, and the documentation design specification.
- Produces: A Chinese primary entrance, equivalent English entrance, and an accurate Unreleased change summary referenced by the documentation hub.

- [ ] **Step 1: Rewrite the Chinese README around the design workflow**

Replace the existing mixed/garbled content with concise sections for project purpose, supported inputs, user journey, quick start, workflow choice, common user questions, capability boundaries, result locations, documentation links, license status, and acknowledgements. Define technical terms in plain language on first use and keep the only startup command:

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

- [ ] **Step 2: Create the matching English README**

Mirror every Chinese section and status row without adding English-only capabilities, commands, caveats, or development detail. Link the two README files to each other near the title.

- [ ] **Step 3: Expand the Unreleased changelog**

Document the upload portal and workflow routing, SRT capability detection and partial-SRT core, pure-rotation experiment, optional CUDA and CPU fallback, SfM-CAD baseline and FOV corrections, viewer/keyframe/upload/storage-root fixes, and the interface-only SRT limitations.

- [ ] **Step 4: Run focused text checks**

Run:

```powershell
rg -n "rank1|GPU BA|bundle adjustment.*GPU|TODO|TBD|<<<<<<<|=======|>>>>>>>" README.md README_EN.md CHANGELOG.md
```

Expected: no undocumented rank1 claim, GPU-BA claim, placeholder, or conflict marker. Review any intentional changelog wording manually.

- [ ] **Step 5: Commit**

```powershell
git add README.md README_EN.md CHANGELOG.md
git commit -m "docs: refresh bilingual project overview"
```

### Task 2: Documentation hub and design references

**Files:**
- Create: `docs/README.md`
- Create: `docs/design/system-architecture.md`
- Create: `docs/design/coordinates-and-alignment.md`

**Interfaces:**
- Consumes: Workflow modules, route status, coordinate/FOV/alignment behavior, and README terminology from Task 1.
- Produces: Stable navigation plus authoritative architecture and coordinate/alignment explanations for later technical documents.

- [ ] **Step 1: Create the documentation hub**

Group links by “使用工作台”, “理解设计”, “部署与维护”, and “历史设计记录”. Mark stage and superpowers documents as historical rather than current operating instructions.

- [ ] **Step 2: Document system architecture**

Describe the workflow portal, viewer, JobRunner/stages, data and run storage, static root versus storage root, workflow state transitions, route modes, external dependencies, and recoverability without presenting interface-only routes as runnable.

- [ ] **Step 3: Document coordinates and alignment**

Define SfM, CAD, web-viewer, and ENU frames; explain oriented Sim3, two-anchor and segmented constraints, manual keyframes, FOV precedence, pure-rotation observability limits, and validation/rejection of degenerate alignment results.

- [ ] **Step 4: Verify navigation and terminology**

Run:

```powershell
rg -n "Stable|Experimental CLI|Experimental|Interface only|Optional|storage-root|FOV|Sim3" docs/README.md docs/design
```

Expected: status and technical terminology match the Global Constraints and all referenced repository-relative files exist.

- [ ] **Step 5: Commit**

```powershell
git add docs/README.md docs/design
git commit -m "docs: add architecture and alignment guides"
```

### Task 3: Detailed engineering and troubleshooting references

**Files:**
- Create: `docs/technical/developer-guide.md`
- Create: `docs/technical/api-and-artifacts.md`
- Create: `docs/technical/troubleshooting.md`

**Interfaces:**
- Consumes: Architecture and coordinate terminology from Task 2 plus actual CLI modules, HTTP routes, manifests, and artifact directories in `main`.
- Produces: Detailed engineering material deliberately omitted from the designer-oriented README.

- [ ] **Step 1: Write the developer guide**

Cover Python 3.10+, editable dependency groups, environment checks, the package/module map, CLI catalogue, frontend locations, local serving, separate `--root`/`--storage-root`, and test commands. Clearly distinguish real/synthetic validation boundaries.

- [ ] **Step 2: Write the API and artifact reference**

Document workflow HTTP endpoints from the server implementation, `dataset_manifest.json`, `job_status.json`, `job_process.json`, input storage, stage directories, reports/logs, safe roots, extra read-only roots, and CAD/SRT import behavior.

- [ ] **Step 3: Write troubleshooting**

Provide symptom → likely cause → check → resolution entries for upload/path failures, CUDA fallback, global BA appearing stalled, CAD import, coordinate-plane mismatch, FOV near 29 degrees, SRT expectations, pure-rotation placement, missing viewer media, keyframe completion controls, and render/road diagnostics.

- [ ] **Step 4: Verify commands and paths**

Run:

```powershell
python -m cadscene.cli.serve_viewer --help
python -m cadscene.cli.check_sfm_environment --help
rg --files src/cadscene/cli apps | Sort-Object
```

Expected: documented command modules and frontend paths exist; option spelling matches command help.

- [ ] **Step 5: Commit**

```powershell
git add docs/technical
git commit -m "docs: add developer and troubleshooting references"
```

### Task 4: Synchronize existing operational documents

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `docs/sfm_cuda_backend.md`
- Modify: `docs/workflow_routing.md`
- Modify: `docs/srt_capability_detection.md`
- Modify: `docs/pipeline_usage.md`
- Modify: `docs/web_viewer_usage.md`

**Interfaces:**
- Consumes: Status vocabulary and technical facts established in Tasks 1–3.
- Produces: Existing topic documents that no longer contradict the current code or new entry points.

- [ ] **Step 1: Update current-versus-future status**

Move upload routing, optional CUDA, partial-SRT CLI core, and pure-rotation execution out of future-only wording. Keep formal SRT JobRunner integration and full-pose execution in future/interface-only status.

- [ ] **Step 2: Correct CUDA and SRT boundaries**

State CPU defaults and fallback behavior, restrict CUDA claims to supported feature extraction/matching, and state that ordinary SRT metadata does not guarantee precision position, attitude, or elevation.

- [ ] **Step 3: Refresh pipeline and viewer usage**

Add the workflow portal entrance, keyframe planning/completion behavior, manual FOV precedence, viewer manifest media recovery, `--storage-root`, and road-diagnostic skip behavior where relevant.

- [ ] **Step 4: Scan synchronized documents**

Run:

```powershell
rg -n "future|planned|CUDA|GPU|SRT|pure.rotation|storage-root|FOV" docs/roadmap.md docs/sfm_cuda_backend.md docs/workflow_routing.md docs/srt_capability_detection.md docs/pipeline_usage.md docs/web_viewer_usage.md
```

Expected: every status reference is compatible with the Global Constraints.

- [ ] **Step 5: Commit**

```powershell
git add docs/roadmap.md docs/sfm_cuda_backend.md docs/workflow_routing.md docs/srt_capability_detection.md docs/pipeline_usage.md docs/web_viewer_usage.md
git commit -m "docs: synchronize workflow and backend status"
```

### Task 5: Whole-document verification and integration

**Files:**
- Modify only documentation files when verification finds a defect.

**Interfaces:**
- Consumes: All documentation from Tasks 1–4.
- Produces: Reviewed, internally linked, test-verified documentation ready to merge and push.

- [ ] **Step 1: Scan all Markdown for corruption and placeholders**

Run:

```powershell
rg -n "锟|�|TODO|TBD|<<<<<<<|=======|>>>>>>>" -g "*.md" .
```

Expected: no mojibake, replacement characters, unresolved placeholders, or conflict markers in current documentation. Historical documents may use explicit completed checklist items but not unresolved placeholders.

- [ ] **Step 2: Validate repository-relative Markdown links**

Run the repository documentation link checker if present; otherwise execute a small read-only Python link scan that ignores `http`, `https`, and anchors and reports missing relative targets.

Expected: zero missing repository-relative link targets in the changed documents.

- [ ] **Step 3: Compare bilingual README structure**

Extract heading levels and tables from both README files and manually confirm one-to-one section, workflow-status, command, and limitation parity.

- [ ] **Step 4: Run the full test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass; pre-existing third-party deprecation warnings may remain.

- [ ] **Step 5: Review and integrate**

Obtain independent review of technical accuracy, Chinese/English parity, designer readability, and the complete branch diff. Fix all Critical/Important findings, rerun affected verification, merge the reviewed branch into `main`, and push `main` to `origin`.
