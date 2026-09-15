# Public release documentation and licensing plan

> **For agentic workers:** Use subagent-driven-development for the documentation task and independent review; controller handles release operations and licensing inventory.

**Goal:** Refresh current user/technical documentation, remove docs/SOP, declare MIT for project-owned code and publish GitHub v0.1.5 with verified deliverables.

**Architecture:** Documentation describes the actual main implementation, not historical plans. Retain historical records with clear archival status. Keep application MIT separate from third-party terms and preserve the tested application behavior.

**Tech Stack:** Markdown, pytest, setuptools, Windows portable packaging, GitHub CLI.

## Global constraints

- Release version is 0.1.5; do not change algorithm or user data.
- Update current Chinese/English README, use instructions and technical references from source evidence.
- Remove docs/SOP and its live links from the current tree, without rewriting Git history.
- Copyright holder is CADScene Workbench contributors; project-owned code uses MIT. Third-party components retain their own licenses.
- Keep old local ZIPs intact; create the public distribution in a new subdirectory.
- Publish only to AlexXiao0517/cadscene_workbench; no source videos, CAD, SRT, project storage, secrets or experiment outputs in Git/Release.
- Binary assets require an explicit dependency/license inventory; disclose any unresolved redistribution issue instead of declaring universal MIT coverage.

## Task 1: Current documentation and SOP removal

- [ ] Inventory tracked documentation and classify current instructions versus historical evidence.
- [ ] Update README.md, README_EN.md, docs/README.md, docs/pipeline_usage.md, docs/roadmap.md, docs/sfm_cuda_backend.md, docs/srt_capability_detection.md, docs/web_viewer_usage.md, docs/workflow_routing.md, docs/design/*.md, docs/technical/*.md, apps/web_camera_viewer/README.md, scripts/README.md, and packaging/windows/使用说明.txt.
- [ ] Mark remaining historical document groups explicitly as historical, without pretending their original designs are current behavior.
- [ ] Remove exact tracked docs/SOP files using apply_patch and remove links to them.
- [ ] Update tests/docs/test_current_documentation.py to validate current workflow claims, absent SOP, and working links; run RED before document changes, then GREEN.
- [x] Independent spec/accuracy review against implementation.

## Task 2: MIT and delivery metadata

- [ ] Add canonical LICENSE with Copyright (c) 2026 CADScene Workbench contributors.
- [ ] Add THIRD_PARTY_NOTICES.md from installed and vendored component evidence, not a claim that dependencies are MIT.
- [ ] Add license metadata to pyproject.toml and ensure wheel includes LICENSE and notices.
- [ ] Extend packaging so fresh portable bundles include current documentation and license files; cover the file contract with tests before modifying the builder.
- [ ] Inspect actual FFmpeg, COLMAP, OpenGV and native runtime redistribution requirements; do not publish a binary whose required materials cannot be established.
- [ ] Run focused documentation, metadata, wheel and packaging tests and full suite before commit/push.

## Task 3: Verified GitHub release

- [ ] Create final review package for changes from bb62b32 and address important findings.
- [ ] Commit approved changes, fast-forward main and push normally to the confirmed origin.
- [ ] Prepare new public assets without overwriting previous local delivery, verify checksums and archived contents.
- [ ] Create v0.1.5 release initially as draft, upload approved artifacts and notes, then publish only after verifying tag target and asset sizes/checksums.
- [ ] Verify remote main/tag/release visibility and deliver links. If binary redistribution remains blocked, clearly report the missing materials rather than silently substituting an incomplete package.

## Execution checkpoint — 2026-09-15

Current documentation has been audited and corrected; ten tracked SOP files were
removed (recoverable in Git history). Canonical MIT, separate vendored notices,
wheel license metadata and portable documentation inclusion are implemented.
The owner authorized original pure-rotation backend code under MIT, without
changing OpenGV or other third-party terms.

Verification: full suite 2105 passed / 4 skipped; after final documentation and
portable-link corrections the focused documentation/packaging suite passed 58
tests. Independent source review found no critical or important issues; its
portable checklist link issue was covered by a failing test and fixed.

Task 3's public release is **not complete**. The user chose to wait and publish
source, wheel and portable assets together. See
`packaging/windows/PUBLIC_RELEASE_CHECKLIST.md` for the outstanding Gyan FFmpeg,
Conda dependency source delivery and COLMAP native notice/provenance checks.
Existing local tester packages remain intact; no public binary approval is
implied by successful application tests.
