# Final review fix report

Date: 2026-07-24

Worktree: `D:\zjic2026\cadscene_workbench\.worktrees\sfm-cuda-bounded-ba`

## Finding 1 — explicit CUDA failure overrides positive text

Test:

`tests/sfm/test_colmap_cli_backend.py::test_gpu_execution_requires_positive_log_evidence`

RED evidence:

```text
assert parse_gpu_execution("SiftGPU not supported; using CPU features", requested=True) is False
E AssertionError: assert True is False
```

Fix:

- `parse_gpu_execution()` now checks `is_cuda_failure(output)` before any positive GPU pattern.
- Existing positive SIFT GPU / worker cases and negative fallback cases remain covered.

GREEN evidence:

```text
tests/sfm/test_colmap_cli_backend.py::test_gpu_execution_requires_positive_log_evidence PASSED
```

## Finding 2 — progress callback failure preserves process semantics

Test:

`tests/sfm/test_colmap_cli_backend.py::test_command_runner_drains_and_waits_after_line_callback_failure`

RED evidence:

```text
E RuntimeError: cannot observe first line
Captured stdout: first line
```

The callback exception escaped immediately, so the remaining two lines were not
drained and `wait()` was not reached.

Fix:

- Treat line observation as best-effort.
- Catch the first callback exception, emit one concise
  `[sfm:progress_warning] progress callback failed; continuing: ...` log line,
  disable further callback invocations, and continue draining stdout.
- Call `process.wait()` and preserve the process return code.
- The regression uses return code `7` and verifies normal
  `ColmapCommandError.result` contents, all three output lines, and `wait_called`.

GREEN evidence:

```text
tests/sfm/test_colmap_cli_backend.py::test_command_runner_drains_and_waits_after_line_callback_failure PASSED
```

## Finding 3 — mapper progress is monotonic

Test:

`tests/sfm/test_colmap_cli_backend.py::test_pipeline_mapper_progress_never_decreases_after_global_ba`

RED evidence:

```text
E assert [0.78, 0.76] == [0.78, 0.78]
```

Fix:

- Track the maximum progress emitted by the mapper line callback.
- Preserve the registration and global-BA thresholds (`0.76`, `0.78`).
- A registration event after global BA now emits `0.78`, not `0.76`.

GREEN evidence:

```text
tests/sfm/test_colmap_cli_backend.py::test_pipeline_mapper_progress_never_decreases_after_global_ba PASSED
```

## Finding 4 — targeted BA propagation coverage

Tests:

- `tests/cli/test_run_sfm_backend_cli.py::test_custom_ba_values_are_recorded_in_manifest_inputs`
- `tests/sfm/test_reconstruction.py::test_colmap_cli_cpu_retry_preserves_all_ba_kwargs`

The requested propagation behavior was already present, so both tests were
initially GREEN (`2 passed in 0.42s`). To prove the tests detect regressions, a
temporary mutation removed `ba_global_max_refinements` from each propagation
site.

Mutation RED evidence:

```text
test_custom_ba_values_are_recorded_in_manifest_inputs FAILED
E KeyError: 'ba_global_max_refinements'

test_colmap_cli_cpu_retry_preserves_all_ba_kwargs FAILED
E KeyError: 'ba_global_max_refinements'

2 failed in 0.54s
```

The original propagation lines were restored exactly.

Restored GREEN evidence:

```text
tests/cli/test_run_sfm_backend_cli.py::test_custom_ba_values_are_recorded_in_manifest_inputs PASSED
tests/sfm/test_reconstruction.py::test_colmap_cli_cpu_retry_preserves_all_ba_kwargs PASSED
2 passed in 0.70s
```

The retry test proves the primary CUDA call and CPU retry receive identical
values for all six keys:

- `ba_global_frames_ratio`
- `ba_global_points_ratio`
- `ba_global_frames_freq`
- `ba_global_points_freq`
- `ba_global_max_num_iterations`
- `ba_global_max_refinements`

## Files changed

- `cadscene/sfm/colmap_cli.py`
- `tests/sfm/test_colmap_cli_backend.py`
- `tests/sfm/test_reconstruction.py`
- `tests/cli/test_run_sfm_backend_cli.py`
- `.superpowers/sdd/final-review-fix-report.md`

No runtime run data was touched and active reconstruction PID 261912 was not
inspected, signaled, or stopped.

## Final verification

Command:

```powershell
python -m pytest tests/sfm/test_colmap_cli_backend.py tests/sfm/test_sfm_backend_detection.py tests/sfm/test_reconstruction.py tests/cli/test_run_sfm_backend_cli.py tests/workflow/test_job_runner_sfm_backend.py -v
```

Final output:

```text
collected 33 items

tests/sfm/test_colmap_cli_backend.py: 9 passed
tests/sfm/test_sfm_backend_detection.py: 6 passed
tests/sfm/test_reconstruction.py: 14 passed
tests/cli/test_run_sfm_backend_cli.py: 2 passed
tests/workflow/test_job_runner_sfm_backend.py: 2 passed

============================= 33 passed in 1.55s ==============================
```

`git diff --check` completed with exit code 0 and no output.

## Self-review

- Scope: only the one production module, three focused test modules, and this
  report changed.
- CUDA/CPU boundary: no mapper or BA GPU options were added; GPU remains limited
  to feature extraction and matching.
- Matching/initialization: no guided-matching or initialization-pair behavior
  changed.
- BA defaults/propagation: defaults are unchanged; both primary and retry calls
  still share the same `cli_kwargs`.
- Process semantics: callback failure no longer masks the real process result;
  nonzero return codes still raise `ColmapCommandError`, and zero returns still
  produce `ColmapCommandResult`.
- Logging: exactly one concise warning is emitted after the first callback
  failure to avoid log spam.
- Progress: the existing `0.76` and `0.78` thresholds are preserved while
  preventing a decrease after global BA.
- Review result: no unresolved correctness, scope, or compatibility concerns
  found.
