(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CadsceneFrameSync = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const VIDEO_ORIGINS = new Set(["video_playback", "video_seek"]);

  function createCoordinator(options = {}) {
    const clampFrame = options.clampFrame || ((frame) => Math.max(0, frame));
    const onLogicalFrame = options.onLogicalFrame || (() => {});
    const seekVideo = options.seekVideo || (() => {});
    const schedule = options.schedule || ((callback, delayMs) => setTimeout(callback, delayMs));
    const cancel = options.cancel || ((handle) => clearTimeout(handle));
    const throttleMs = Number.isFinite(Number(options.throttleMs))
      ? Math.max(0, Number(options.throttleMs))
      : 100;

    let logicalFrame = normalizeFrame(options.initialFrame ?? 0);
    let pendingFrame = null;
    let pendingOrigin = null;
    let scheduledSeek = null;
    let disposed = false;

    function normalizeFrame(frame) {
      const numeric = Number(frame);
      const rounded = Number.isFinite(numeric) ? Math.round(numeric) : 0;
      const clamped = Number(clampFrame(rounded));
      return Number.isFinite(clamped) ? Math.round(clamped) : 0;
    }

    function clearScheduledSeek() {
      if (scheduledSeek !== null) cancel(scheduledSeek);
      scheduledSeek = null;
    }

    function runPendingSeek() {
      scheduledSeek = null;
      if (disposed || pendingFrame === null) return;
      const frame = pendingFrame;
      const origin = pendingOrigin || "timeline_drag";
      pendingFrame = null;
      pendingOrigin = null;
      seekVideo(frame, origin);
    }

    function selectFrame(frame, origin = "unknown", selectionOptions = {}) {
      if (disposed) return logicalFrame;
      logicalFrame = normalizeFrame(frame);
      onLogicalFrame(logicalFrame, origin);

      const media = selectionOptions.media || "none";
      if (VIDEO_ORIGINS.has(origin) || media === "none") return logicalFrame;
      if (media === "immediate") {
        clearScheduledSeek();
        pendingFrame = null;
        pendingOrigin = null;
        seekVideo(logicalFrame, origin);
        return logicalFrame;
      }
      if (media === "throttled") {
        pendingFrame = logicalFrame;
        pendingOrigin = origin;
        if (scheduledSeek === null) scheduledSeek = schedule(runPendingSeek, throttleMs);
      }
      return logicalFrame;
    }

    function flushVideoSeek(origin = "timeline_release") {
      if (disposed || pendingFrame === null) return logicalFrame;
      clearScheduledSeek();
      const frame = pendingFrame;
      pendingFrame = null;
      pendingOrigin = null;
      seekVideo(frame, origin);
      return frame;
    }

    function dispose() {
      clearScheduledSeek();
      pendingFrame = null;
      pendingOrigin = null;
      disposed = true;
    }

    return {
      selectFrame,
      flushVideoSeek,
      currentFrame: () => logicalFrame,
      dispose,
    };
  }

  return { createCoordinator };
});
