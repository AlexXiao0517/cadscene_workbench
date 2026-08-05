# Pure-rotation known-video intrinsics recovery

## Problem

The pure-rotation POC requires a same-resolution COLMAP camera candidate on its
`--cadscene-readonly` search path. Newly uploaded projects do not contain
`cameras.txt` or `cameras.bin`, so the POC exits at its first decoded frame.

The current uploaded `永康北互通.MP4` has SHA-256
`db2bd19a6478af436b809801f72dddaa5528bf7f761c57964efcba132fec851b`. This
exactly matches the audited July full-video run, whose stored summary records
the candidate intrinsics used to process it successfully.

## Scope and design

1. Add a small, pure-rotation-only known-video calibration registry. It maps
   only that SHA-256 to the audited 1920x1080 `OPENCV` candidate parameters.
2. Before launching the external POC, calculate the uploaded video's SHA-256.
   If it is registered, create a private temporary `cameras.txt` containing
   that candidate and pass that directory as `--cadscene-readonly`.
3. The temporary directory is created below the run's staging directory and is
   deleted with that staging directory. It is never written to the dataset,
   worktree root, or any user development module.
4. An unregistered video retains the existing behavior: the POC searches the
   configured read-only root. If no candidate is found, it fails explicitly;
   no historical calibration is silently reused.
5. Preserve the external backend stdout and stderr in the failed run directory
   so a later backend failure is diagnosable from the UI/job log.

## Tests

- A registered file fingerprint produces a camera candidate with the recorded
  dimensions and intrinsics.
- A non-matching file gets no candidate directory and cannot receive the
  historical calibration.
- The CLI passes the generated candidate directory only for the registered
  input.
- A non-zero backend exit leaves captured stdout/stderr accessible to the
  caller.

## Boundaries

- No change to SfM, alignment, quality, rendering, portal routing, or the
  user's existing worktree changes.
- The calibration remains marked unverified by the external POC; this restores
  the prior exploratory workflow, not a claim of general calibration validity.
