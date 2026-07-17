# Branch fix 2 report

## Scope

- Legacy dataset manifests that provide `srt.status` but omit `workflow` now infer `srt_sfm_fused` for `partial` and `srt_full_pose` for `full`. Both inferred modes are normalized to `interface_only`.
- Direct `run-stage` requests load that normalized manifest and remain blocked before a job status is created.
- Incremental SRT parsing now bounds retained unterminated-line and block data. Oversized malformed input is discarded with a `RuntimeWarning`; parsing resumes after the next newline or blank block separator.

## TDD evidence

The new regression tests were run before the production fix and failed as expected: legacy manifests became `sfm_only/ready`, direct stage launch returned 200, and hostile streams emitted no bounded-discard warning. After the implementation, the same focused tests passed.

## Verification

`python -m pytest tests/srt tests/workflow/test_data_import.py tests/cli/test_serve_viewer_workflow_api.py -q`

Result: 42 passed (one pre-existing third-party deprecation warning).
