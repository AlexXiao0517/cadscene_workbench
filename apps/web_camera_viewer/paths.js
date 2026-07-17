(function () {
  "use strict";

  const DEFAULT_DATASET = "dataset";

  function param(params, name, aliases) {
    const names = [name].concat(aliases || []);
    for (const key of names) {
      const value = params.get(key);
      if (value && value.trim()) return value.trim();
    }
    return "";
  }

  function runPath(dataset, runId, stage, fileName) {
    return `/runs/${encodeURIComponent(dataset)}/${encodeURIComponent(runId)}/${stage}/${fileName}`;
  }

  function datasetDefaults(dataset, runId) {
    const encoded = encodeURIComponent(dataset);
    const defaults = {
      video: `/data/${encoded}/video/${encoded}.mp4`,
      videoFallbacks: [`/data/${encoded}/${encoded}.mp4`],
      cad: `/data/${encoded}/cad/design.json`,
      cadFallbacks: [`/data/${encoded}/design.json`],
      track: `/data/${encoded}/tracks/camera_track.json`,
      trackFallbacks: [],
      camera: "/configs/camera_params.json",
      review: "",
      suggestions: "",
      qualityTimeline: "",
      sfmScene: "",
      diagnosticsScene: "",
      qualityTrack: "",
    };
    if (!runId) return defaults;
    return {
      ...defaults,
      track: runPath(dataset, runId, "03_alignment", "camera_track_pred.json"),
      trackFallbacks: [runPath(dataset, runId, "01_keyframes", "camera_track_manual.json")],
      qualityTrack: runPath(dataset, runId, "04_quality", "camera_track_pred_quality.json"),
      qualityTimeline: runPath(dataset, runId, "04_quality", "quality_timeline.csv"),
      suggestions: runPath(dataset, runId, "04_quality", "keyframe_suggestions.json"),
      sfmScene: runPath(dataset, runId, "05_viewer_scene", "sfm_viewer_scene.json"),
      diagnosticsScene: runPath(dataset, runId, "06_road_surface", "viewer_diagnostics_scene.json"),
    };
  }

  // 保留旧 viewer 调用的 resolveViewerPaths() 接口。
  // 优先级：显式 URL 参数 > dataset + runId 推导 > /data/<dataset>/ 默认。
  function resolveViewerPaths() {
    const params = new URLSearchParams(window.location.search);
    const datasetName = param(params, "dataset") || DEFAULT_DATASET;
    const runId = param(params, "runId", ["run_id"]);
    const base = datasetDefaults(datasetName, runId);
    const paths = {
      dataset: datasetName,
      runId,
      video: param(params, "video") || base.video,
      videoFallbacks: param(params, "video") ? [] : base.videoFallbacks,
      cad: param(params, "cad") || base.cad,
      cadFallbacks: param(params, "cad") ? [] : base.cadFallbacks,
      track: param(params, "track") || base.track,
      trackFallbacks: param(params, "track") ? [] : base.trackFallbacks,
      qualityTrack: param(params, "qualityTrack", ["quality_track"]) || base.qualityTrack,
      camera: param(params, "camera") || base.camera,
      review: param(params, "review") || base.review,
      suggestions: param(params, "suggestions") || base.suggestions,
      qualityTimeline: param(params, "qualityTimeline", ["quality_timeline"]) || base.qualityTimeline,
      sfmScene: param(params, "sfmScene", ["sfm_scene"]) || base.sfmScene,
      diagnosticsScene: param(params, "diagnosticsScene", ["diagnostics_scene"]) || base.diagnosticsScene,
    };
    for (const key of ["video", "cad"]) {
      if (!paths[key]) console.warn(`[cadscene viewer] missing ${key} path; pass ?${key}=... or place data under /data/`);
    }
    return paths;
  }

  window.resolveViewerPaths = resolveViewerPaths;
})();
