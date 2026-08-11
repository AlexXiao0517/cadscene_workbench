# Workbench Saved Progress Resume Design

## Problem

Reopening a clip after a validated workbench save currently republishes the raw trajectory attempt over the viewer run directory, creates a blank editing session, and always routes a trajectory-ready session to the debugging stage. The immutable workbench output still exists, but the UI appears to lose progress.

## Required behavior

- Treat the latest validated immutable workbench output matching the current project, clip, workflow, clip revision, trajectory revision, and trajectory fingerprint as the resume baseline.
- Reopening must preserve the baseline output revision and fingerprint in the new editable session and clip reference.
- Do not replace an existing viewer run that already contains the saved result. If the run is missing or was previously overwritten, rebuild it from the validated raw trajectory and then restore the immutable saved camera track.
- Pure Rotation restore publishes the saved track as the current placement track. SfM restore publishes it as the current manual camera track.
- Resume stage is server-derived: a validated saved output resumes at render/preview; trajectory-only clips resume at debugging/keyframes; clips without trajectory resume at workflow start.
- Closing a resumed session without a new save keeps the clip's previous saved result active.
- A later save from the resumed editing session creates a new immutable output revision; it never mutates the prior revision.
- If no matching valid immutable output exists, fail closed or resume from the current trajectory only. Never silently attach stale output from different inputs.

## Recovery

The session store exposes a read-only lookup for the latest saved session matching the current workbench context. This supports recovery when an older buggy reopen already replaced the clip reference with an empty editing session. Every candidate is validated against the immutable output manifest and artifact checksum before use.

## Testing

API tests cover normal reopen, recovery after a overwritten reference/run, stage selection, preservation on close, and rejection of stale or corrupt saved outputs. Existing workbench, project API, and full test suites remain the regression gate.
