(function (root, factory) {
  const exports = factory();
  if (typeof module === "object" && module.exports) module.exports = exports;
  if (root?.document) root.CadsceneAnnotationPts = exports.createBrowserAuthority(root);
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function sourcePtsAtTime(frames, clipTimeSec) {
    if (!Array.isArray(frames) || frames.length === 0) return null;
    const target = Number(clipTimeSec);
    let low = 0;
    let high = frames.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(frames[middle].clip_time_sec) < target) low = middle + 1;
      else high = middle;
    }
    const after = frames[low];
    const before = low > 0 ? frames[low - 1] : after;
    const selected = Math.abs(Number(before.clip_time_sec) - target)
      <= Math.abs(Number(after.clip_time_sec) - target) ? before : after;
    return Number.isInteger(selected.source_pts) ? selected.source_pts : null;
  }

  function clipTimeAtSourcePts(frames, sourcePts) {
    if (!Array.isArray(frames) || frames.length === 0) return null;
    const target = Number(sourcePts);
    if (!Number.isFinite(target)) return null;
    let low = 0;
    let high = frames.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(frames[middle].source_pts) < target) low = middle + 1;
      else high = middle;
    }
    const after = frames[low];
    const before = low > 0 ? frames[low - 1] : after;
    const selected = Math.abs(Number(before.source_pts) - target)
      <= Math.abs(Number(after.source_pts) - target) ? before : after;
    const clipTime = Number(selected.clip_time_sec);
    return Number.isFinite(clipTime) ? clipTime : null;
  }

  function sourceFrameAtTime(frames, clipTimeSec) {
    if (!Array.isArray(frames) || frames.length === 0) return null;
    const target = Number(clipTimeSec);
    if (!Number.isFinite(target)) return null;
    let low = 0;
    let high = frames.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(frames[middle].clip_time_sec) < target) low = middle + 1;
      else high = middle;
    }
    const after = frames[low];
    const before = low > 0 ? frames[low - 1] : after;
    const selected = Math.abs(Number(before.clip_time_sec) - target)
      <= Math.abs(Number(after.clip_time_sec) - target) ? before : after;
    return Number.isInteger(selected.source_frame_index)
      ? selected.source_frame_index : null;
  }

  function clipTimeAtSourceFrame(frames, sourceFrameIndex) {
    if (!Array.isArray(frames) || frames.length === 0) return null;
    const target = Number(sourceFrameIndex);
    if (!Number.isFinite(target)) return null;
    let low = 0;
    let high = frames.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (Number(frames[middle].source_frame_index) < target) low = middle + 1;
      else high = middle;
    }
    const after = frames[low];
    const before = low > 0 ? frames[low - 1] : after;
    const selected = Math.abs(Number(before.source_frame_index) - target)
      <= Math.abs(Number(after.source_frame_index) - target) ? before : after;
    const clipTime = Number(selected.clip_time_sec);
    return Number.isFinite(clipTime) ? clipTime : null;
  }

  function hidden(reason, extra = {}) {
    return { visible: false, reason, ...extra };
  }

  function videoTrackVisual(annotation, results, sourcePts, sourceToDisplay, screenOffset = null, sourceBounds = null) {
    function insideSource(anchor, bbox = null) {
      if (!Array.isArray(sourceBounds)) return true;
      const width = Number(sourceBounds[0]);
      const height = Number(sourceBounds[1]);
      if (!(width > 0 && height > 0)) return true;
      if (anchor[0] < 0 || anchor[0] >= width || anchor[1] < 0 || anchor[1] >= height) return false;
      if (!Array.isArray(bbox)) return true;
      return Number(bbox[0]) >= 0 && Number(bbox[1]) >= 0
        && Number(bbox[0]) + Number(bbox[2]) <= width
        && Number(bbox[1]) + Number(bbox[3]) <= height;
    }
    const result = results instanceof Map
      ? results.get(sourcePts)
      : (Array.isArray(results)
        ? results.find((item) => item.source_pts === sourcePts) : null);
    if (!result) {
      const initialization = annotation.anchor?.initialization;
      if (!annotation.active_tracking_revision && Number(initialization?.source_pts) === sourcePts) {
        const bbox = initialization?.bbox;
        const initial = initialization?.anchor_xy || (
          Array.isArray(bbox) ? [Number(bbox[0]) + Number(bbox[2]) / 2, Number(bbox[1]) + Number(bbox[3]) / 2] : null
        );
        if (initial && insideSource(initial, bbox)) {
          const offset = screenOffset || annotation.screen_offset || [0, 0];
          const anchor = sourceToDisplay({ x: Number(initial[0]), y: Number(initial[1]) });
          const label = sourceToDisplay({
            x: Number(initial[0]) + Number(offset[0]),
            y: Number(initial[1]) + Number(offset[1]),
          });
          if (anchor && label) {
            return {
              visible: true,
              reason: "initialization_preview",
              anchor_source_xy: [Number(initial[0]), Number(initial[1])],
              label_source_xy: [
                Number(initial[0]) + Number(offset[0]),
                Number(initial[1]) + Number(offset[1]),
              ],
              anchor_xy: [anchor.x, anchor.y],
              label_xy: [label.x, label.y],
              tracking_status: "untracked",
              confidence: 1,
            };
          }
        }
      }
      return hidden("no_exact_tracking_pts");
    }
    const confidence = Number(result.confidence || 0);
    const trackingStatus = String(result.tracking_status || "lost");
    const minimum = Number(annotation.visibility_policy?.min_tracking_confidence ?? 0.5);
    if (
      !result.visibility
      || !result.anchor_xy
      || !new Set(["initialized", "tracked"]).has(trackingStatus)
      || confidence < minimum
    ) {
      return hidden("tracking_lost", {
        tracking_status: trackingStatus,
        confidence,
      });
    }
    const offset = screenOffset || annotation.screen_offset || [0, 0];
    const anchorSource = { x: Number(result.anchor_xy[0]), y: Number(result.anchor_xy[1]) };
    if (!insideSource([anchorSource.x, anchorSource.y], result.bbox)) {
      return hidden("outside_viewport");
    }
    const labelSource = {
      x: anchorSource.x + Number(offset[0]),
      y: anchorSource.y + Number(offset[1]),
    };
    const anchor = sourceToDisplay(anchorSource);
    const label = sourceToDisplay(labelSource);
    if (!anchor || !label) return hidden("outside_viewport");
    return {
      visible: true,
      reason: "visible",
      anchor_source_xy: [anchorSource.x, anchorSource.y],
      label_source_xy: [labelSource.x, labelSource.y],
      anchor_xy: [anchor.x, anchor.y],
      label_xy: [label.x, label.y],
      tracking_status: trackingStatus,
      confidence,
    };
  }

  function cadAnchorCreationDecision(projected) {
    if (!projected || typeof projected !== "object") {
      return { ok: false, reason: "projection_unavailable" };
    }
    if (!projected.visible || !Array.isArray(projected.source_xy)) {
      return { ok: false, reason: String(projected.reason || "projection_invalid") };
    }
    return { ok: true, reason: "visible" };
  }

  function cadAnchorVisual(annotation, projected, sourceToDisplay, screenOffset = null) {
    const decision = cadAnchorCreationDecision(projected);
    if (!decision.ok) return hidden(decision.reason);
    const offset = screenOffset || annotation.screen_offset || [0, 0];
    const anchorSource = {
      x: Number(projected.source_xy[0]),
      y: Number(projected.source_xy[1]),
    };
    const labelSource = {
      x: anchorSource.x + Number(offset[0]),
      y: anchorSource.y + Number(offset[1]),
    };
    const anchor = sourceToDisplay(anchorSource);
    const label = sourceToDisplay(labelSource);
    if (!anchor || !label) return hidden("outside_viewport");
    return {
      visible: true,
      reason: "visible",
      anchor_source_xy: [anchorSource.x, anchorSource.y],
      label_source_xy: [labelSource.x, labelSource.y],
      anchor_xy: [anchor.x, anchor.y],
      label_xy: [label.x, label.y],
      depth_m: Number(projected.depth_m),
    };
  }

  function createBrowserAuthority(browser) {
    const video = browser.document.querySelector("#sourceVideo");
    let projectId = "";
    let clipId = "";
    let frames = [];
    let sourceTimeBase = null;
    const tracking = new Map();

    function sourceToDisplay(point) {
      return browser.cadsceneVideoDisplayTransform?.sourceToDisplay?.(point) || null;
    }

    function inPtsRange(annotation, sourcePts) {
      const range = annotation.source_pts_range;
      return Number.isInteger(sourcePts)
        && sourcePts >= Number(range?.start_pts)
        && sourcePts < Number(range?.end_pts_exclusive);
    }

    return {
      async configure(nextProjectId, nextClipId) {
        projectId = String(nextProjectId || "");
        clipId = String(nextClipId || "");
        const response = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clipId)}/annotation-preview`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error("无法加载标签的权威 PTS 映射");
        const payload = await response.json();
        if (payload.timestamp_authority !== "source_decoded_frame_integer_pts") {
          throw new Error("标签 PTS 映射不是权威解码帧数据");
        }
        frames = payload.frames || [];
        const numerator = Number(payload.time_base?.numerator);
        const denominator = Number(payload.time_base?.denominator);
        sourceTimeBase = Number.isInteger(numerator) && numerator > 0
          && Number.isInteger(denominator) && denominator > 0
          ? { numerator, denominator }
          : null;
        browser.dispatchEvent(new browser.CustomEvent("cadscenePtsAuthorityReady", {
          detail: { projectId, clipId },
        }));
        return payload;
      },

      currentSourcePts() {
        return sourcePtsAtTime(frames, Number(video?.currentTime || 0));
      },

      sourceFrameAtTime(clipTimeSec) {
        return sourceFrameAtTime(frames, clipTimeSec);
      },

      clipTimeAtSourceFrame(sourceFrameIndex) {
        return clipTimeAtSourceFrame(frames, sourceFrameIndex);
      },

      hasFrameMap() {
        return frames.length > 0;
      },

      sourceTimeBase() {
        return sourceTimeBase ? { ...sourceTimeBase } : null;
      },

      seekSourcePts(sourcePts) {
        const clipTime = clipTimeAtSourcePts(frames, sourcePts);
        if (clipTime === null || !video) return null;
        video.currentTime = clipTime;
        return clipTime;
      },

      async loadTrackingRevisions(annotations) {
        for (const annotation of annotations || []) {
          const revision = annotation.active_tracking_revision;
          if (!revision || tracking.has(revision)) continue;
          const response = await fetch(
            `/api/projects/${encodeURIComponent(projectId)}/annotations/${encodeURIComponent(annotation.annotation_id)}/tracking`,
            { cache: "no-store" },
          );
          if (!response.ok) continue;
          const payload = await response.json();
          if (payload.tracking_revision === revision) {
            tracking.set(
              revision,
              new Map((payload.results || []).map((item) => [Number(item.source_pts), item])),
            );
          }
        }
      },

      visualFor(annotation, options = {}) {
        const sourcePts = this.currentSourcePts();
        if (!annotation.user_visible) return hidden("user_hidden");
        if (!inPtsRange(annotation, sourcePts)) return hidden("outside_pts_range");
        if (annotation.anchor_type === "video_track") {
          return videoTrackVisual(
            annotation,
            tracking.get(annotation.active_tracking_revision) || [],
            sourcePts,
            sourceToDisplay,
            options.screenOffset,
            [video.videoWidth, video.videoHeight],
          );
        }
        const projected = browser.cadsceneProjectCadWorldPoint?.(
          annotation.anchor?.cad_world_xyz,
        );
        return cadAnchorVisual(
          annotation,
          projected,
          sourceToDisplay,
          options.screenOffset,
        );
      },

      displayDeltaToSource(dx, dy) {
        const origin = sourceToDisplay({ x: 0, y: 0 });
        const unit = sourceToDisplay({ x: 1, y: 1 });
        const scaleX = unit && origin ? unit.x - origin.x : 1;
        const scaleY = unit && origin ? unit.y - origin.y : 1;
        return { x: Number(dx) / scaleX, y: Number(dy) / scaleY };
      },

      sourceDeltaToDisplay(dx, dy) {
        const origin = sourceToDisplay({ x: 0, y: 0 });
        const unit = sourceToDisplay({ x: Number(dx), y: Number(dy) });
        return {
          x: unit && origin ? unit.x - origin.x : Number(dx),
          y: unit && origin ? unit.y - origin.y : Number(dy),
        };
      },

      sourceToDisplayPoint(point) {
        return sourceToDisplay({ x: Number(point[0]), y: Number(point[1]) });
      },
    };
  }

  return {
    sourcePtsAtTime,
    clipTimeAtSourcePts,
    sourceFrameAtTime,
    clipTimeAtSourceFrame,
    videoTrackVisual,
    cadAnchorVisual,
    cadAnchorCreationDecision,
    createBrowserAuthority,
  };
});
