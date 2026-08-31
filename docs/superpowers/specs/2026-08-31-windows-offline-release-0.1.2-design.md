# Windows Offline Release 0.1.2 Design

## Goal

Build a reproducible `CADScene-0.1.2` Windows offline tester bundle containing the already verified workflow fixes, while explicitly excluding the unverified adjacent-clip up/down scene-bridging feature.

## Release baseline and allowlist

The release branch starts from `9858176`, which already contains the portable Windows launcher, bundled runtime rules, compact project identifiers, compact Windows publication paths, and the first SfM FOV refresh repair.

Only these application changes are added from `codex/async-job-progress-scene-positioning`:

- queued trajectory status over stale outputs;
- SfM FOV restoration after reopening a workbench;
- cancelled trajectory batches returning to pending;
- synchronized trajectory progress inside and outside the workbench;
- durable workbench resume state, authoritative source PTS restoration, merged fitted/manual camera anchors, and unsaved-draft warning.

Commits that add adjacent-clip locating, scene overlap windows, scene bridge candidates, up/down bridging, or bridge publication are excluded. The release source must not add or modify `cadscene/projects/scene_bridges.py`, `cadscene/projects/scene_bridge_runner.py`, or scene-bridge UI controls relative to the Windows baseline.

## Version and packaging

`pyproject.toml` and `packaging/windows/release-config.json` use version `0.1.2`; the bundle directory and ZIP are named `CADScene-0.1.2`. A fresh wheel is built from the release branch and installed into a dedicated conda-pack runtime. The existing pinned Pure Rotation backend and OpenGV binary versions remain unchanged.

The generated `launcher/release.json` records the release branch commit used to build the wheel. The old `CADScene-0.1.0` directory and archive are not deleted during assembly.

## SfM FOV contract

SfM writes camera intrinsics into `02_sfm/camera_trajectory.json`. `/api/workflow/sfm-camera-init` derives horizontal FOV from `width` and `fx`. On entering or reopening the workbench, a single frame-0 default camera track without a source is treated as a placeholder and may receive this FOV. A real manual or fitted track remains authoritative and is never overwritten.

Verification uses a non-70-degree fixture and checks the Python FOV loader, the workbench static contract, the wheel contents, and the installed offline bundle files. A real project API smoke confirms that the packaged server returns the same computed FOV.

## Verification

- Focused FOV, workbench resume, project progress, Windows path, and packaging tests.
- Full pytest regression.
- Dependency and whitespace checks.
- Wheel inspection for static assets and resume modules.
- Offline bundle verifier, launcher doctor, and HTTP startup smoke.
- Explicit diff audit proving scene-bridge functionality was not introduced.
