(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CadsceneKeyframes = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const confirmedManualSources = new Set([
    "manual_keyframe",
    "confirmed_keyframe",
    "manual",
    "confirmed",
    "manual_anchor",
    "manual_corrected",
    "scene_boundary_anchor",
    "scene_overlap_anchor",
  ]);

  function isConfirmedManualKeyframe(item) {
    return Boolean(
      item
      && item.camera
      && typeof item.camera === "object"
      && confirmedManualSources.has(String(item.source || "")),
    );
  }

  function confirmedManualKeyframes(keyframes) {
    const candidates = (Array.isArray(keyframes) ? keyframes : []).filter(
      (item) => item && item.camera && typeof item.camera === "object",
    );
    const explicit = candidates.filter(isConfirmedManualKeyframe);
    if (explicit.length > 0) return explicit;
    const legacy = candidates.filter((item) => !String(item.source || ""));
    return legacy.length >= 2 ? legacy : [];
  }

  function mergeFittedTrackWithManual(fittedTrack, manualTrack) {
    const fitted = fittedTrack && typeof fittedTrack === "object" ? fittedTrack : null;
    const manual = manualTrack && typeof manualTrack === "object" ? manualTrack : null;
    if (!fitted) return manual;
    if (!manual) return fitted;
    const usesPosePriorResiduals = fitted?.meta?.pose_prior_schema === "srt_pose_prior_v1"
      || manual?.meta?.pose_prior_schema === "srt_pose_prior_v1";
    if (
      manual?.meta?.workflow === "srt_full_pose"
      && manual?.meta?.authoritative_workbench_track === true
      && !usesPosePriorResiduals
    ) return manual;

    const fittedFrames = Array.isArray(fitted.keyframes) ? fitted.keyframes : [];
    if (fittedFrames.length === 0) return manual;
    const manualFrames = confirmedManualKeyframes(manual.keyframes);
    const byFrame = new Map();
    for (const keyframe of fittedFrames) {
      const frame = Number(keyframe?.frame);
      if (!Number.isFinite(frame) || !keyframe?.camera) continue;
      byFrame.set(frame, keyframe);
    }
    for (const keyframe of manualFrames) {
      const frame = Number(keyframe?.frame);
      if (!Number.isFinite(frame)) continue;
      // 同一源帧始终以用户最后确认的人工关键帧为准。
      byFrame.set(frame, keyframe);
    }
    return {
      ...fitted,
      keyframes: [...byFrame.values()].sort(
        (left, right) => Number(left.frame) - Number(right.frame),
      ),
    };
  }

  return {
    confirmedManualKeyframes,
    isConfirmedManualKeyframe,
    mergeFittedTrackWithManual,
  };
});
