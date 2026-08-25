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

  return { confirmedManualKeyframes, isConfirmedManualKeyframe };
});
