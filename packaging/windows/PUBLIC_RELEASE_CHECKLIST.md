# Public release gate — 0.1.5

Status checked: 2026-09-15. **Public release pending.** This is an engineering
distribution checklist, not a legal clearance opinion. The owner requested that
source, Python wheel and portable ZIP be published together after the required
third-party materials are ready. Do not publish an incomplete substitute.

## Project-owned code

- CADScene Workbench uses the root MIT license.
- The owner also authorized original `pure_rotation_camera_poc` code under MIT.
  The pinned source is `85ab6404bfb5a07da8cdaaba0a1e5c4da10dc250`.
  A commit identifier alone is not a downloadable source delivery: export the
  actual build sources, excluding reports and user data, before distribution.
- OpenGV stays under its own BSD terms, snapshot
  `91f4b19c73450833a40e463ad3648aae80b3a7f3`. Preserve its license, and the notices
  for any pybind11 or toolchain runtime included in the backend.

## Outstanding portable-material checks

| Payload | Identified version | Still required |
| --- | --- | --- |
| imageio-ffmpeg embedded executable | Gyan FFmpeg 7.1 essentials, GPL-enabled static build | Recover historical library versions, corresponding source, patches and build materials; otherwise omit this duplicate executable from a separately verified public runtime. Wrapper BSD text does not cover the executable. |
| Conda FFmpeg | 8.1.2 `gpl_he504192_904` | Archive matching source, six recipe patches, build scripts and GPL texts; index dependency sources/notices against the DLLs actually shipped. The developer's package cache is not recipient delivery. |
| COLMAP CUDA | 4.1.0 `fa8e3b3` | Record official archive hash and match every staged file; include COLMAP source/license and version-matched DLL/plugin notices, especially ONNX Runtime. Establish any CUDA redistribution conditions from build/linkage evidence, not filenames alone. |
| Remaining native runtime | Exact final environment | Generate a package/file manifest and preserve applicable source and notice materials for each shipped dependency. |

Conda FFmpeg's recorded source input is
`https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.gz`, SHA-256
`32faba5ef67340d54724941eae1425580791195312a4fd13bf6f820a2818bf22`;
the recipe identifies feedstock commit
`9827350937da796f204f9a760d6a7e9e4260fddb`.
This identifies inputs; it does not assert that source delivery is complete.

See [third-party notices](../../THIRD_PARTY_NOTICES.md) and
[FFmpeg's upstream licensing guidance](https://ffmpeg.org/legal.html).

## Final publication checks

- [ ] Resolve all material gaps above for the exact final binary payload.
- [ ] Review source export for private paths, reports, credentials and user data.
- [ ] Build into a new output directory; preserve previous local delivery ZIPs.
- [ ] Include current docs, MIT and indexed third-party notices in the portable ZIP.
- [ ] Verify wheel licenses and ensure no native runtime or user assets are in it.
- [ ] Verify extracted portable startup, dependency checks and rendering after any
  runtime change; compare output with the accepted version.
- [ ] Publish source companions, hashes and dependency manifest with the binaries.
- [ ] Verify main/tag commit, uploaded hashes and release notes before making
  `v0.1.5` public.

The existing local tester ZIP is not the final public artifact. Do not overwrite
it or describe a build verification result as redistribution approval.

## Isolated public-runtime preparation

`assemble --runtime-profile conda-ffmpeg-only` opts into the audited 0.1.5
FFmpeg deduplication profile. It verifies the replacement and duplicate hashes,
removes only the known imageio FFmpeg executable, repairs its relocation record,
and writes `runtime-profile.json`. An existing staging directory or ZIP is
rejected; ZIP creation is exclusive, including collisions during assembly.
Omitting the profile preserves the original builder behavior. This profile is
version-specific and must not be reused with an unaudited runtime.

The profile is not a license-completeness assertion: its manifest deliberately
keeps `redistribution_status` as `pending`. Source and notice delivery, final
dependency inventory, startup and rendering tests remain separate release gates.
Legacy toolchain exclusions and the optional ONNX CUDA provider exclusion are
separate archive-preparation steps, not implicit effects of this FFmpeg option.
Keep their file/hash manifests with the final release materials. Supported
COLMAP GPU SIFT must remain available and be tested after those changes.

The user approved an external Microsoft prerequisite on 2026-09-15. Public
bundles must also use `--external-msvc-manifest` with the audited exclusion
input. Windows 10/11 supplies UCRT/API sets; recipients install the x64 Visual
C++ runtime from Microsoft. The public launcher checks signed installed DLLs
before Python starts and creates four local wheel-loader aliases. Never ship
those generated aliases or repackage a launched directory. A pristine ZIP and
a separate extracted validation copy are required. The old local offline ZIP
remains untouched and must not be substituted for this public artifact.
