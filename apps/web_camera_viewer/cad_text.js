(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CadsceneCadText = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function isStationLabel(label) {
    const text = String(label?.text || "");
    const layer = String(label?.layer || "");
    return label?.text_role === "station"
      || /(?:^|\b)(?:[A-Z]{0,3})?K?\d+\+\d+(?:\.\d+)?(?:$|\b)/i.test(text)
      || /(桩号|station|chainage)/i.test(layer);
  }

  function finiteOption(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function cadRotationToSceneZ(degrees) {
    return finiteOption(degrees, 0) * Math.PI / 180;
  }

  function labelPriority(entry) {
    const entity = entry?.entity || entry || {};
    return (isStationLabel(entity) ? 1_000_000 : 0)
      + Math.max(0, Number(entity.cad_height) || 0) * 1_000;
  }

  function createSpatialLabelIndex(entries, options = {}) {
    const maxCellsPerAxis = Math.max(
      1,
      Math.floor(finiteOption(options.maxCellsPerAxis, 32)),
    );
    const getPoint = typeof options.getPoint === "function"
      ? options.getPoint
      : (entry) => entry?.worldPoint || entry?.entity?.world_position;
    const validEntries = [];
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const entry of entries || []) {
      const point = getPoint(entry);
      const x = Number(point?.[0]);
      const y = Number(point?.[1]);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      validEntries.push({ entry, x, y });
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x);
      maxY = Math.max(maxY, y);
    }
    if (validEntries.length === 0) {
      return { cellCount: 0, collect: () => [] };
    }

    const cellWidth = Math.max((maxX - minX) / maxCellsPerAxis, 1e-9);
    const cellHeight = Math.max((maxY - minY) / maxCellsPerAxis, 1e-9);
    const cellMap = new Map();
    for (const item of validEntries) {
      const ix = Math.min(maxCellsPerAxis - 1, Math.floor((item.x - minX) / cellWidth));
      const iy = Math.min(maxCellsPerAxis - 1, Math.floor((item.y - minY) / cellHeight));
      const key = iy * maxCellsPerAxis + ix;
      let cell = cellMap.get(key);
      if (!cell) {
        cell = {
          center: [minX + (ix + 0.5) * cellWidth, minY + (iy + 0.5) * cellHeight],
          items: [],
        };
        cellMap.set(key, cell);
      }
      cell.items.push(item.entry);
    }
    const cells = [...cellMap.values()];
    for (const cell of cells) {
      cell.items.sort((left, right) => labelPriority(right) - labelPriority(left));
    }

    function collect(cellSelector, maxItems = 5000) {
      const limit = Math.max(0, Math.floor(finiteOption(maxItems, 5000)));
      if (limit === 0 || typeof cellSelector !== "function") return [];
      const selectedCells = [];
      for (const cell of cells) {
        const result = cellSelector(cell);
        if (result === false || result === null || result === undefined) continue;
        const score = result === true ? 0 : Number(result);
        if (Number.isFinite(score)) selectedCells.push({ cell, score, index: 0 });
      }
      selectedCells.sort((left, right) => right.score - left.score);
      const selectedItems = [];
      let active = selectedCells;
      while (active.length > 0 && selectedItems.length < limit) {
        const next = [];
        for (const state of active) {
          selectedItems.push(state.cell.items[state.index]);
          state.index += 1;
          if (state.index < state.cell.items.length) next.push(state);
          if (selectedItems.length >= limit) break;
        }
        active = next;
      }
      return selectedItems;
    }

    return { cellCount: cells.length, collect };
  }

  function selectProjectedLabels(candidates, options = {}) {
    const width = Math.max(finiteOption(options.width, 1), 1);
    const height = Math.max(finiteOption(options.height, 1), 1);
    const cellSize = Math.max(finiteOption(options.cellSize, 84), 1);
    const maxLabels = Math.max(0, Math.floor(finiteOption(options.maxLabels, 240)));
    const margin = Math.max(0, finiteOption(options.margin, 0.08));
    if (maxLabels === 0) return [];

    const occupiedCells = new Map();
    for (const candidate of candidates || []) {
      const visible = (
        Number.isFinite(candidate.x)
        && Number.isFinite(candidate.y)
        && Number.isFinite(candidate.depth)
        && candidate.depth >= -1
        && candidate.depth <= 1
        && candidate.x >= -margin * width
        && candidate.x <= (1 + margin) * width
        && candidate.y >= -margin * height
        && candidate.y <= (1 + margin) * height
      );
      if (!visible) continue;
      const priority = (isStationLabel(candidate.entity) ? 1_000_000 : 0)
        + Math.max(0, Number(candidate.entity?.cad_height) || 0) * 1_000
        - Math.hypot(candidate.x - width / 2, candidate.y - height / 2);
      const cell = `${Math.floor(candidate.x / cellSize)}:${Math.floor(candidate.y / cellSize)}`;
      const previous = occupiedCells.get(cell);
      if (!previous || priority > previous.priority) {
        occupiedCells.set(cell, { candidate, priority });
      }
    }
    return [...occupiedCells.values()]
      .sort((left, right) => right.priority - left.priority)
      .slice(0, maxLabels)
      .map((entry) => entry.candidate);
  }

  function createLruCache(limit, dispose = () => {}) {
    const capacity = Math.max(0, Math.floor(finiteOption(limit, 0)));
    const values = new Map();

    function get(key) {
      if (!values.has(key)) return undefined;
      const value = values.get(key);
      values.delete(key);
      values.set(key, value);
      return value;
    }

    function remove(key) {
      if (!values.has(key)) return false;
      const value = values.get(key);
      values.delete(key);
      dispose(value);
      return true;
    }

    function set(key, value) {
      if (values.has(key)) {
        const previous = values.get(key);
        values.delete(key);
        if (previous !== value) dispose(previous);
      }
      if (capacity === 0) {
        dispose(value);
        return value;
      }
      values.set(key, value);
      while (values.size > capacity) {
        remove(values.keys().next().value);
      }
      return value;
    }

    function clear() {
      for (const value of values.values()) dispose(value);
      values.clear();
    }

    return {
      get,
      set,
      delete: remove,
      clear,
      get size() { return values.size; },
    };
  }

  return {
    isStationLabel,
    selectProjectedLabels,
    createLruCache,
    cadRotationToSceneZ,
    createSpatialLabelIndex,
  };
});
