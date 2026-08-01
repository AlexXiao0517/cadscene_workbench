# Video Analysis Segmentation Implementation Plan

> **For agentic workers:** Execute inline with strict red-green-refactor TDD; commit each independently testable task.

**Goal:** Add an isolated backend core that analyzes uploaded video by authoritative source PTS, emits explainable logical clips of at most 60 seconds, and recommends, but never runs, an existing workflow.

**Architecture:** A new `cadscene.video_analysis` package owns FFmpeg-backed PTS probing/sparse decoding, per-window visual evidence, boundary fusion, motion hysteresis, duration planning, SRT coverage recommendation, revision-safe manifests, and atomic publication. A standalone CLI invokes it without changing existing workflow routing or algorithms.

**Tech Stack:** Python 3.10+, NumPy, OpenCV, FFmpeg resolved from PATH or `imageio-ffmpeg`, pytest.

## Global Constraints

- Source packet/frame PTS is authoritative; never derive frame identity from `time * fps`.
- Analysis is low-resolution and sparse; no COLMAP and no full-video OpenGV trajectory solve.
- Logical clips target 45-55 s, never exceed 60 s, normally avoid clips below 8 s, and never cross a confident discontinuity.
- Outputs are staged, validated, and atomically published under `02_video_analysis/`; reanalysis creates a new immutable `analysis_revision` directory and does not touch future user overrides.
- Existing workflow selection and execution remain unchanged.

### Task 1: Contracts, authoritative PTS, synthetic fixtures, and atomic outputs

- [ ] RED: test schemas, exact irregular PTS preservation, source/clip PTS mapping, six required artifacts, immutable revisions, and failed-publication rollback.
- [ ] GREEN: implement models, FFmpeg resolver/probe/sparse decoder, mappings, serializers, validator, publisher, and minimal CLI surface.
- [ ] VERIFY: focused tests plus workflow tests; commit independently.

### Task 2: Multi-signal shot/discontinuity detection

- [ ] RED: synthesize hard-cut, black/exposure, weak-feature/coverage, homography/flow break, and decode/PTS anomaly cases; assert cuts do not rely on histograms alone.
- [ ] GREEN: implement per-pair evidence and explainable fused boundaries with configurable thresholds.
- [ ] VERIFY: focused tests and synthetic concatenation video; commit independently.

### Task 3: Motion hysteresis, duration segmentation, SRT recommendation, and orchestration

- [ ] RED: test four motion classes, sustained general/rotation transitions, short-static absorption, preferred/forced cuts, SRT precedence, low-confidence review, and old-short-video single clips.
- [ ] GREEN: implement sliding-window classification, hysteresis, boundary planner, recommendation, report generation, and end-to-end analyzer.
- [ ] VERIFY: focused, workflow-related, and synthetic mode-switch tests; commit independently.

### Task 4: Real-video acceptance, performance, and regression

- [ ] Analyze the current repository video with measured wall time and peak RSS; inspect every clip range, mode, reason, recommendation, and review flag.
- [ ] Run focused, workflow, related, and full test suites; audit scope and clean status; commit only test fixtures/docs needed for reproducibility.
