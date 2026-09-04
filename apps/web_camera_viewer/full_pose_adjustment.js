(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CadsceneFullPoseAdjustment = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function finiteOffset(value) {
    const offset = {
      x: Number(value?.x ?? 0),
      y: Number(value?.y ?? 0),
      z: Number(value?.z ?? 0),
    };
    if (!Object.values(offset).every(Number.isFinite)) {
      throw new Error("full-pose route offset must contain finite values");
    }
    return offset;
  }

  function offsetFromTrack(track) {
    const saved = track?.meta?.route_offset_xyz_m;
    if (!Array.isArray(saved) || saved.length !== 3) return { x: 0, y: 0, z: 0 };
    return finiteOffset({ x: saved[0], y: saved[1], z: saved[2] });
  }

  function applyAbsoluteOffset(track, previousOffset, nextOffset) {
    if (!track || typeof track !== "object" || !Array.isArray(track.keyframes)) {
      throw new Error("full-pose camera track is unavailable");
    }
    const previous = finiteOffset(previousOffset);
    const next = finiteOffset(nextOffset);
    const scale = Number(track?.meta?.cad_scale ?? 1);
    if (!Number.isFinite(scale) || scale <= 0) {
      throw new Error("full-pose camera track cad_scale must be positive");
    }
    const delta = {
      x: (next.x - previous.x) / scale,
      y: (next.y - previous.y) / scale,
      z: (next.z - previous.z) / scale,
    };
    const shifted = JSON.parse(JSON.stringify(track));
    shifted.meta = {
      ...(shifted.meta || {}),
      route_offset_xyz_m: [next.x, next.y, next.z],
      authoritative_workbench_track: true,
    };
    shifted.keyframes = shifted.keyframes.map((keyframe) => {
      if (!keyframe?.camera) return keyframe;
      return {
        ...keyframe,
        camera: {
          ...keyframe.camera,
          x: Number(keyframe.camera.x) + delta.x,
          y: Number(keyframe.camera.y) + delta.y,
          z: Number(keyframe.camera.z) + delta.z,
        },
      };
    });
    return { track: shifted, offset: next };
  }

  return { applyAbsoluteOffset, offsetFromTrack };
});
