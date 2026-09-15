# Video Project Pipeline SDD Progress

Plan: `docs/superpowers/plans/2026-08-03-video-project-pipeline.md`
Base: `c907b3e`

Task 1: complete (commits 8d03980..7a9835a, review clean)
Task 2: complete (commits 7a9835a..2444b29, review clean)
Task 3: complete (commits 2444b29..55758a4, review clean; 689 passed, 1 skipped)
Task 4: complete (commits 55758a4..59c0c2b, final review clean; 852 passed, 1 skipped)
Task 5: complete (commits 59c0c2b..837cda3, final review clean; 916 passed, 1 skipped)
Task 6: in progress

## Public runtime release adjustment — 2026-09-15

Plan: docs/superpowers/plans/2026-09-15-public-runtime-adjustment.md
Base: 608b4a8
Profile implementation: complete in working diff, 52 packaging tests passed,
final whole-change review r5 clean. Includes prefix-record repair and exclusive
ZIP creation. Final full suite: 2,121 passed, 4 skipped; pending commit.
Candidate C: cold unpack/install, doctor, isolated HTTP startup/stop through
Windows PowerShell passed; 12 synthetic decoded frames match baseline exactly.
Curated COLMAP: 275 hash-matched retained files, optional ONNX CUDA provider
omitted; GPU SIFT extraction/matching passed.
Publication: pending GCC/MSYS2 source and Microsoft runtime terms, final material
index/assembly, final artifact verification. Do not publish a partial release.

## External Microsoft runtime — 2026-09-15

User approved official end-user installation prerequisite. Plan:
docs/superpowers/plans/2026-09-15-external-microsoft-runtime.md
Base main/worktree: d690205 (pushed). Existing service and ZIP preserved.
Baseline: 52 packaging tests pass. Task 1 bootstrap complete and independently
reviewed clean; main confirmed 69 tests pass plus real signed-System32 probe.
Task 2 audited exclusion/no-repack guard in progress.
System32 provides signed x64 VC runtime 14.51.36247.0 on this test host;
public minimum is 14.51.36231.0. No installer has been run by this task.
GCC 15.2.0 MSYS2 Rev8 source, recipe and all patches are now collected and
hash verified. Microsoft binaries will be excluded, not redistributed.
Inventory: 112 VC/UCRT DLLs excluded, 12 canonical System32 requirements and
4 hashed aliases (pycolmap + pyproj). ONNX MIT DLLs are not MSVC exclusions.
Fresh archive .local/public-015-external-runtime.tar.gz SHA256
d633528e83fb5ee25e58e1022b480701eba5ebb5c25f37b01552b36f0a499931;
49,756 members, all 112 exclusions and legacy toolchain absence verified.
Task 2: complete (uncommitted atop d690205, independent review clean after
canonical-path, reparse, output-manifest and default-profile fixes).
Main: full suite 2166 passed / 7 skipped / one pre-existing fontTools warning;
final affected suites 98 passed / 3 link-privilege skips. Real Windows junction
created via PowerShell and rejected by Python 3.11 lstat guard.
Source companion: 3139 indexed files, full ZIP CRC and every SHA256 verified.
Task 3: integrating pristine bundle and separate extracted launch/render test.
