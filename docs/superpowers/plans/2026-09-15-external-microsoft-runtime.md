# External Microsoft Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task with TDD and independent review.

**Goal:** Publish a Windows bundle without redistributing Microsoft runtime binaries; users obtain prerequisites directly from Microsoft.

**Architecture:** A public-only JSON manifest describes prerequisites and wheel-specific DLL aliases. A PowerShell bootstrap checks installed x64 Microsoft runtime files before Python starts, then creates required aliases locally from those installed files. The builder removes only hash-audited Microsoft payload from new staging and refuses to archive a bootstrapped directory containing restored DLLs.

**Tech Stack:** Python packaging, Windows PowerShell 5.1, pytest subprocess tests.

## Global Constraints

- User approved official end-user prerequisite acquisition on 2026-09-15.
- Do not change the original 8310 service, original runtime or existing ZIP.
- No automatic download, installation, elevation or license acceptance.
- No algorithm, trajectory, camera, rendering or user-project changes.
- Default non-public packaging and launch behavior remain unchanged.
- Require Windows x64 and Microsoft VC runtime at least 14.51.36231.0.
- Official user-facing prerequisite page: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
- Manifest filename: `external-msvc.json`, schema version 1.
- Manifest schema: `{"schema_version":1,"minimum_version":"14.51.36231.0","required_dlls":["vcruntime140.dll","vcruntime140_1.dll","msvcp140.dll","msvcp140_1.dll","msvcp140_2.dll","msvcp140_atomic_wait.dll","msvcp140_codecvt_ids.dll","concrt140.dll","vcomp140.dll"],"aliases":[{"source":"msvcp140.dll","target":"runtime/Lib/site-packages/pycolmap.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll"},{"source":"msvcp140_2.dll","target":"runtime/Lib/site-packages/pycolmap.libs/msvcp140_2-fe68f6b61d8de0f75e5717671051586a.dll"},{"source":"vcomp140.dll","target":"runtime/Lib/site-packages/pycolmap.libs/vcomp140-f96f3a14d88d8846f31f3ab38a490304.dll"}],"excluded":[{"path":"runtime/vcruntime140.dll","sha256":"<actual audited digest in release input>"}]}`. The release input contains the complete audited exclusion list, not this abbreviated example.

## Task 1: Pre-Python prerequisite bootstrap

Inventory correction: the final alias list also contains source `msvcp140.dll`
with target `runtime/Lib/site-packages/pyproj.libs/msvcp140-d76d4b45e040cbc263297f5a5893a46c.dll`.
There are four required aliases in this release, not just the three pycolmap
entries shown in the schema example. Permit only pycolmap.libs/pyproj.libs
targets with the matching canonical source and hashed suffix.
The final required canonical DLL list additionally includes `vcamp140.dll`,
`vccorlib140.dll` and `vcruntime140_threads.dll`, matching the removed native
runtime set. All twelve are supplied by the installed x64 VC runtime.

**Files:** create `packaging/windows/launcher/external-msvc.ps1`, `tests/packaging/test_external_msvc_launcher.py`; modify `packaging/windows/launcher/start.ps1` and `packaging/windows/使用说明.txt`.

**Interface:** dot-source helper, call `Initialize-ExternalMsvc -BundleRoot $bundleRoot`. With no manifest it is a no-op. It must run before the first Python invocation and any workspace migration. Tests invoke real Windows PowerShell in subprocess; helper functions may be overridden only in tests for OS probes.

- [ ] RED: absent manifest makes no changes; missing installed DLL returns clear Chinese error with official URL and no copies; outdated/wrong-architecture/untrusted signer likewise refuses; valid installed runtime creates exactly four aliases; unchanged valid aliases are idempotent; traversal, unknown sources, link/reparse ancestors and targets refuse before writes. Assert launcher call precedes unpack/Python.
- [ ] Implement functions to validate manifest, source DLL basename allowlist, exact alias target shape, relative path containment and reparse points. System source directory comes from the OS, not PATH or user downloads. Validate installed version, PE x64 and Microsoft Authenticode signature before writes. Resolve all requirements first so missing dependencies do not partially initialize.
- [ ] Copy only manifest-listed alias DLLs, retaining original bytes, from installed System32. Use a uniquely named temporary file and atomic replacement for an older existing regular alias. Never alter installed system files. Print that local aliases must not be redistributed/repacked. Do not create aliases for UCRT/API sets, which Windows supplies.
- [ ] GREEN: `python -m pytest tests/packaging/test_external_msvc_launcher.py tests/packaging/test_windows_offline_bundle.py -q`; syntax-load PowerShell helper; update usage text to distinguish public prerequisite-based package from old offline package.
- [ ] Independent review; main commits after verification.

## Task 2: Audited exclusion and no-repack guard

**Files:** create `scripts/external_msvc.py`, `tests/packaging/test_external_msvc_bundle.py`; modify `scripts/windows_offline_bundle.py`.

**Interface:** add `--external-msvc-manifest PATH`, valid only together with `--runtime-profile conda-ffmpeg-only`. New helper validates the manifest and hashes every present exclusion before removing anything. Already excluded archive members are allowed. Preserve/extract ordinary runtime only into a new staging directory. Remove corresponding literal conda-unpack prefix records; never execute the script to inspect them. Save the validated manifest at bundle root.

- [ ] RED: known files are omitted and manifest written; hash mismatch/unsafe path/duplicate path/unknown Microsoft basename fail before deletion; missing file is allowed; relocation records for omitted runtime files are removed; unrelated records survive. Existing output/ZIP remains protected.
- [ ] Implement exact manifest validation and audited removal only after full preflight. Support Microsoft runtime canonical/hashed DLL names and UCRT/API-set DLLs; inventories for unrelated build tools must be excluded while preparing the input archive, not passed as arbitrary delete rules.
- [ ] Guard `write_zip64` when the root contains `external-msvc.json`: any listed excluded file or generated alias present rejects archive creation before opening output. Reject unexpected Microsoft runtime DLL names encountered in payload. This prevents accidental redistribution after local smoke tests.
- [ ] GREEN covering tests; audit final source archive membership and PE inventory. Record each exclusion's path/hash and retain source/notice materials separately.

## Task 3: Integrated release verification

- [ ] Build into a new short path with audited public runtime/COLMAP profiles, fresh wheel and complete third-party materials. Do not overwrite previous candidates.
- [ ] Produce the pristine archive before test launch, then test a separate extraction. Verify no excluded DLL exists in pristine archive.
- [ ] Through actual Windows PowerShell entry points, verify prerequisite failure path, installed prerequisite startup/doctor, stop, GPU SIFT and synthetic render comparison. Do not install anything on the host.
- [ ] Full pytest suite, final whole-change review, merge/push to main, rebuild final artifact metadata from the final commit.
- [ ] Review public source/material exports for user data and private artifacts; publish all intended assets together only after exact hashes/materials gates pass.
