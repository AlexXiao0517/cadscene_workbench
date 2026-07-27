# Pure-rotation experimental workflow — implementation plan

1. Add explicit no-SRT hovering selection, manifest metadata, and compatibility tests.
2. Add an isolated subprocess-only POC adapter with typed failures and atomic artifacts.
3. Validate/convert POC local rotations, then build fixed-centre CAD placement and correction SLERP tracks.
4. Expose dedicated CLI/API/job operations and minimal viewer/upload controls.
5. Run synthetic/unit, workflow regression, isolation, and read-only real-media smoke checks; commit each task separately.

Boundaries: no POC import/copy/change; no SfM fallback or modification; no inferred translation; no automatic hover detection.
