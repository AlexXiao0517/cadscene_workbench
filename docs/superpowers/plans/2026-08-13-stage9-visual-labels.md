# Stage 9 Visual Labels — Implementation Plan

**Base:** `origin/main` at `4305107022fdfc1a138b40f364e91e2366ab2170`  
**Branch:** `feature/stage9-visual-labels`  
**Worktree:** `D:\zjic2026\cadscene_workbench\.worktrees\stage9-visual-labels`

## Architecture contracts

- Add `cadscene.annotations` as an independent domain. Persist one atomic
  `annotations_manifest.json` per project and immutable artifacts below
  `annotations/<clip_id>/<annotation_id>/<tracking_revision>/`.
- Annotation CRUD uses `expected_revision` and `operation_id`. Annotation edits
  invalidate only the affected clip render. They never invalidate trajectory.
- CAD anchors reuse `cadscene.cad.projection.project_point`, existing
  `CameraState`, and authoritative trajectory/source-PTS lookup.
- Video tracking is behind `VideoAnchorTracker`. V1 is OpenCV pyramidal LK
  sparse flow with forward/backward validation and RANSAC partial-affine ROI
  propagation. Lost/out-of-bounds/low-confidence samples are persisted with
  `visibility=false`; evaluation never bridges a lost interval.
- Preview and render evaluate annotations by integer source decoded-frame PTS
  plus exact time base. Render reads the existing frame map and preserves its
  frame count and ordering byte-for-byte.

## TDD delivery

1. **Annotation domain and API**
   - Add schema, validation, atomic repository, service, snapshot/API routes,
     CRUD tests, optimistic concurrency tests, and render-only invalidation.
   - Commit: `feat: add project annotation domain`
2. **CAD anchor projection**
   - Add PTS-aware CAD anchor evaluator using the existing projection module;
     test front/behind/viewport/time-range and Base/Corrected trajectory inputs.
   - Commit: `feat: project CAD anchored labels`
3. **Video tracking revisions**
   - Add tracker protocol, LK/RANSAC backend, decoded-frame PTS reader,
     immutable tracking repository, correction/re-anchor segments, and lost
     diagnostics. Test forward/backward, boundary exit, lost gaps, revision
     replacement, and old-revision retention.
   - Commit: `feat: track video label anchors`
4. **Workbench annotation editing**
   - Extend the existing viewer with label mode, CAD raycast point selection,
     video point/ROI selection, text/style/time controls, drag-to-offset,
     delete/show/hide, correction/re-anchor, and confidence/lost display.
   - Commit: `feat: edit visual labels in workbench`
5. **PTS-synchronised preview**
   - Load annotation/tracking state into the current viewer; map preview time
     only through authoritative frame-map PTS and share visibility semantics
     with the backend evaluator.
   - Commit: `feat: preview labels by source PTS`
6. **Rendered overlay**
   - Draw evaluated labels directly into each existing render frame without
     changing frame count, PTS, frame map, or concat partition. Preserve the
     no-annotation fast path.
   - Commit: `feat: burn visual labels into clip renders`
7. **Real smoke**
   - Run one real CAD anchor and one real tracked target through preview/render;
     force/observe lost, add re-anchor, and verify hide/resume behavior.
   - Commit only reusable smoke fixtures/assertions if needed.
8. **Regression**
   - Verify all workflow types, project lifecycle, workbench recovery,
     no-label rendering, exact render-frame-map equality, concat coverage, and
     the full pytest suite. Request independent code review before handoff.

