# Task 2 — Persist SRT analysis and extend the dataset manifest

## Changed files

- `cadscene/workflow/data_import.py` — adds streamed `.srt` import, persisted
  JSON/Markdown analysis reports, manifest SRT/workflow defaults, safe SRT
  filename validation, persisted-analysis loading, and isolated analysis
  failures.
- `tests/workflow/test_data_import.py` — adds manifest/default, analysis
  persistence, safe-path, and video/CAD failure-isolation coverage.
- `.superpowers/sdd/task-2-report.md` — this implementation report.

## RED / GREEN evidence

### RED

```text
python -m pytest tests/workflow/test_data_import.py -v
ImportError: cannot import name 'import_srt' from cadscene.workflow.data_import
```

The failing focused test module failed at collection for the intended missing
public API before production code was added.

### GREEN

```text
python -m pytest tests/workflow/test_data_import.py -v
14 passed, 1 warning in 0.44s
```

### Required combined verification

```text
python -m pytest tests/workflow/test_data_import.py tests/srt/test_capability.py -v
22 passed, 1 warning in 0.47s

git diff --check
exit 0
```

The one warning is the pre-existing `fontTools.misc.py23` deprecation warning.

## Self-review

- SRT uploads use the existing chunked stream copier and are only accepted for
  case-insensitive `.srt` filenames validated by the shared safe-filename
  guard.
- Analysis reads the just-persisted telemetry file, then writes JSON and
  Markdown reports atomically.
- New manifests, existing manifests re-saved through `create_dataset`, and
  imported manifests carry explicit missing-SRT `sfm_only` / `ready` defaults.
- An analysis exception records `srt.status=failed` and restores the workflow
  default without changing ready video or CAD metadata.
- Only Task 2 source, tests, and this report are intended for the commit. No
  SfM, alignment, quality, viewer scene, road diagnostics, render protocols,
  dependencies, real inputs, `project/`, `data/`, or `runs/` files changed.

## Concerns

- The SRT parser intentionally reports malformed or insufficient metadata as a
  non-throwing `sfm_only` analysis with warnings. Such uploads are persisted as
  `partial`; only actual write/analysis exceptions become `failed`.

## Commit

`Persist SRT upload analysis in manifests` (final local commit; hash recorded in task handoff).

## Review-fix addendum

### Finding addressed

`_ensure_srt_workflow` previously added only missing top-level blocks. A
legacy manifest with a partial `srt` or `workflow` block therefore still
returned an incomplete public schema, while `list_datasets` did not normalize
at all.

### RED / GREEN evidence

```text
python -m pytest tests/workflow/test_data_import.py -v
2 failed, 14 passed
```

The failures showed the incomplete `{"status": "partial"}` SRT block and
the absent SRT block returned by `list_datasets`.

```text
python -m pytest tests/workflow/test_data_import.py -v
16 passed, 1 warning in 0.44s
```

### Fix

Recursive default merging now fills omitted nested SRT attitude-source fields
and omitted workflow fields without overwriting supplied values or unknown
metadata. `load_dataset_manifest` persists a normalized legacy manifest;
`list_datasets` returns normalized manifests without turning a listing into a
bulk write operation.
