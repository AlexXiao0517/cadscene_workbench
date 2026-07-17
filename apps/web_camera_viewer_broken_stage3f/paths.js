(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);

  function getParam(name) {
    const value = params.get(name);
    return value && value.trim() ? value.trim() : null;
  }

  function joinRunPath(dataset, runId, stage, fileName) {
    return `../../runs/${encodeURIComponent(dataset)}/${encodeURIComponent(runId)}/${stage}/${fileName}`;
  }

  function buildViewerPaths() {
    const dataset = getParam("dataset") || "dataset";
    const runId = getParam("runId");
    const derived = runId
      ? {
          track: joinRunPath(dataset, runId, "03_alignment", "camera_track_pred.json"),
          qualityTrack: joinRunPath(dataset, runId, "04_quality", "camera_track_pred_quality.json"),
          qualityTimeline: joinRunPath(dataset, runId, "04_quality", "quality_timeline.csv"),
          suggestions: joinRunPath(dataset, runId, "04_quality", "keyframe_suggestions.json"),
          sfmScene: joinRunPath(dataset, runId, "05_viewer_scene", "sfm_viewer_scene.json"),
          diagnosticsScene: joinRunPath(dataset, runId, "06_road_surface", "viewer_diagnostics_scene.json"),
        }
      : {};

    return {
      dataset,
      runId,
      video: getParam("video") || null,
      cad: getParam("cad") || null,
      track: getParam("track") || derived.track || null,
      qualityTrack: getParam("qualityTrack") || derived.qualityTrack || null,
      qualityTimeline: getParam("qualityTimeline") || derived.qualityTimeline || null,
      suggestions: getParam("suggestions") || derived.suggestions || null,
      sfmScene: getParam("sfmScene") || derived.sfmScene || null,
      diagnosticsScene: getParam("diagnosticsScene") || derived.diagnosticsScene || null,
    };
  }

  window.CADSCENE_VIEWER_PATHS = buildViewerPaths();
})();
