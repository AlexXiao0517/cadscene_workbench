(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CadsceneCalloutLayout = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const clamp = (value, minimum, maximum) => Math.min(maximum, Math.max(minimum, value));

  function layoutCallout(payload) {
    const [anchorX, anchorY] = payload.anchor_xy.map(Number);
    const [offsetX, offsetY] = payload.screen_offset.map(Number);
    const [requestedWidth, requestedHeight] = payload.panel_size.map(Number);
    const [viewportWidth, viewportHeight] = payload.viewport_size.map(Number);
    const margin = Number(payload.safe_margin);
    const elbow = Number(payload.elbow_length);
    const usableWidth = Math.max(1, viewportWidth - 2 * margin);
    const usableHeight = Math.max(1, viewportHeight - 2 * margin);
    const width = Math.min(requestedWidth, usableWidth);
    const height = Math.min(requestedHeight, usableHeight);
    const left = clamp(anchorX + offsetX - width / 2, margin, viewportWidth - margin - width);
    const top = clamp(anchorY + offsetY - height / 2, margin, viewportHeight - margin - height);
    const right = left + width;
    const bottom = top + height;
    let edgeX = clamp(anchorX, left, right);
    let edgeY = clamp(anchorY, top, bottom);
    let elbowPoint;
    if (anchorX < left) elbowPoint = [edgeX - elbow, edgeY];
    else if (anchorX > right) elbowPoint = [edgeX + elbow, edgeY];
    else if (anchorY < top) elbowPoint = [edgeX, edgeY - elbow];
    else if (anchorY > bottom) elbowPoint = [edgeX, edgeY + elbow];
    else {
      const sides = [
        [Math.abs(anchorX - left), "left"],
        [Math.abs(right - anchorX), "right"],
        [Math.abs(anchorY - top), "top"],
        [Math.abs(bottom - anchorY), "bottom"],
      ].sort((a, b) => a[0] - b[0] || a[1].localeCompare(b[1]));
      const side = sides[0][1];
      if (side === "left") { edgeX = left; elbowPoint = [left - elbow, edgeY]; }
      else if (side === "right") { edgeX = right; elbowPoint = [right + elbow, edgeY]; }
      else if (side === "top") { edgeY = top; elbowPoint = [edgeX, top - elbow]; }
      else { edgeY = bottom; elbowPoint = [edgeX, bottom + elbow]; }
    }
    return {
      anchor_xy: [anchorX, anchorY],
      panel_rect: [left, top, width, height],
      leader_points: [[anchorX, anchorY], elbowPoint, [edgeX, edgeY]],
    };
  }

  return { layoutCallout };
});
