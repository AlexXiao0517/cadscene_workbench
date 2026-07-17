# Task 3 — Server SRT API and workflow status

## Changed files

- `cadscene/cli/serve_viewer.py`
- `tests/cli/test_serve_viewer_upload_api.py`
- `tests/cli/test_serve_viewer_workflow_api.py`

## Delivered behavior

- Adds streamed `POST /api/workflow/upload-srt?dataset=<dataset>[&runId=<run_id>]`
  through the existing multipart parser and the existing `import_srt` safety
  boundary.
- Adds `GET /api/workflow/srt-analysis?dataset=<dataset>` with JSON 404 errors
  when analysis has not been persisted.
- Both successful SRT endpoints return exactly `ok`, `dataset`, `srt_status`,
  `trajectory_mode`, `analysis`, and `message`.
- Partial and full-pose modes update only the upload-stage message to state
  that activation is pending. They do not call `JobRunner.start_stage` or
  create SfM/fusion artifacts.
- Existing video/CAD endpoints retain their manifest response, and no-SRT
  manifests retain `sfm_only` / `ready` defaults.

## TDD evidence

Initial API tests failed as expected because `upload-srt` was unregistered and
returned 404. The full-pose regression was then added before its status branch;
it failed because the reply reported the SfM-only message rather than pending
activation. The minimal full-pose branch made it pass.

## Verification

```text
python -m pytest tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_workflow_api.py -v
16 passed, 1 warning in 9.08s

git diff --check
exit 0
```

The warning is the pre-existing `fontTools.misc.py23` deprecation warning.

## Concerns

No fusion or pose algorithm is started by these APIs. Full runtime-suite
verification was intentionally not run; this task's requested focused API
verification passed.

## Reviewer P2 follow-up

`GET /api/workflow/srt-analysis` now requires a nonblank `dataset` query
parameter. Regression coverage verifies both the absent query and
`?dataset=` return JSON `400` with exactly
`{"error": "missing query parameter: dataset"}`. Before the validation
change, the new test observed the incorrect JSON `404`; after the minimal
change, the focused CLI API suites reported `17 passed, 1 warning`.
