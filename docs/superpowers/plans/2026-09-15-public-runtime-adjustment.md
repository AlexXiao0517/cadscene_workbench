# Public runtime adjustment checkpoint

User authorization: adjust otherwise untraceable dependencies for a separate
public distribution and revalidate rendering; preserve the existing service and
local ZIP. Source, wheel and portable Release assets are still to be published
together only after the material gate passes.

Base: `608b4a82b41a95678d80cbadf4636532f015ee43`.

## Constraints

- No algorithm, user-project or running-service changes.
- Do not overwrite existing delivery directories or ZIPs.
- Retain required GPU sparse reconstruction capability; no silent CPU fallback.
- Native licenses remain separate from application MIT. A passing application
  test is not redistribution clearance.
- Do not publish user images/video/CAD/SRT or local probe databases.

## Work and evidence

1. **Implemented and task-reviewed:** opt-in `conda-ffmpeg-only` staging profile.
   It verifies audited hashes, removes the known duplicate imageio FFmpeg only,
   synchronizes its conda-unpack prefix record using AST/literal inspection,
   rejects unfamiliar structures and existing destinations, and records hashes
   with `redistribution_status=pending`. Default packaging remains unchanged.
   Tests: 50 packaging tests passed after real cold-start discovery and repair.
2. **Materials being assembled:** copied cached notices/recipes for 202 Conda
   records, downloaded recipe-hash-verified codec/Qt/source archives, exported
   pinned original backend and OpenGV sources, and recovered COLMAP official
   archive and dependency provenance. Not all remaining materials are complete.
3. **COLMAP check:** local archive SHA-256 matches the GitHub release asset;
   all 276 extracted files match. GPU SIFT extraction and matching passed with
   CUDA/Conda paths removed. This is not a clean-VM test. Optional ONNX GPU
   provider requirements must not be confused with the SIFT execution path.
4. **Candidate validation:** first isolated candidate exposed the stale
   conda-unpack record; it is retained for inspection, not release. Candidate B
   passed conda-unpack, wheel installation and the packaged renderer smoke.
   Its 12 decoded synthetic frames exactly match the original-runtime smoke
   (maximum frame MAE 0). This is not a customer-video or clean-VM test.
5. **Legacy dependency audit complete:** import-table inspection of 1,312 PE
   files outside the legacy toolchain found only the unused XCB island importing
   its DLLs. Public-only archive preparation therefore excludes
   `Library/mingw-w64/*`, `Library/bin/msys-xcb*`,
   `Library/bin/msys-Xau-6.dll` and `Library/bin/msys-Xdmcp-6.dll` through
   conda-pack, which generates matching relocation records. The original
   environment is unchanged. Archive membership verified: 6,189 files / about
   506 MiB excluded. Candidate C passed cold-start, all doctor checks and HTTP
   startup on isolated port 8415. PowerShell 7 JSON timestamp conversion caused
   the stop helper to reject identity; after exact executable/start-time checks,
   only the test PID was stopped manually. A repeat through the supported
   Windows PowerShell entry point passed both startup and stop. Its 12-frame synthetic
   render also matched the baseline exactly. The original 8310 service stayed
   available. Separate curated COLMAP (275 retained files with matching hashes,
   optional ONNX CUDA provider omitted) passed GPU SIFT extraction and matching.
   Final bundle must use this curated COLMAP payload and be verified again.
6. **Remaining gate:** resolve unavailable legacy toolchain sources or prove and
   verify their exclusion in the public runtime; finish exact native notices and
   source delivery. Then broad review, full tests, fresh final bundle/hash checks
   and only then GitHub publication.

Development materials are under ignored local release-preparation directories;
they are not public assets until inspected and assembled explicitly.

Validation checkpoint: final full suite passed 2,121 tests with 4 skips and one
pre-existing fontTools deprecation warning. The fix passed 52 packaging tests; final independent
review r5 approved both preflight and exclusive ZIP creation, with no remaining
findings. Publication remains pending
the final runtime materials and assembled artifact gates.

Remaining authorization check: Microsoft documents VC runtime redistribution
as limited to licensed Visual Studio users. The runtime-only EULA collected from
Conda does not establish that grant for the publisher. Do not infer permission
merely because DLLs appeared in upstream wheels. Establish the applicable grant
or obtain approval for a release requiring users to obtain Microsoft runtime
components directly. GCC 15.2.0 MSYS2 Rev8 source/recipe collection is underway.
