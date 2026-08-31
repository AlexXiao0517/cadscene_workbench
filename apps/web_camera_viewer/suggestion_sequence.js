(function initSuggestionSequence(globalScope) {
  "use strict";

  function frameOf(item) {
    return Number(item?.frame_index);
  }

  function ordered(items) {
    return (Array.isArray(items) ? items : [])
      .filter((item) => Number.isFinite(frameOf(item)))
      .slice()
      .sort((left, right) => frameOf(left) - frameOf(right));
  }

  function next(items, currentFrame) {
    const sequence = ordered(items);
    if (!sequence.length) return null;
    const current = Number(currentFrame);
    if (!Number.isFinite(current)) return sequence[0];
    const index = sequence.findIndex((item) => frameOf(item) === current);
    return index < 0 ? sequence[0] : sequence[(index + 1) % sequence.length];
  }

  function firstAfter(items, frame) {
    const sequence = ordered(items);
    if (!sequence.length) return null;
    const anchor = Number(frame);
    if (!Number.isFinite(anchor)) return sequence[0];
    return sequence.find((item) => frameOf(item) > anchor) || sequence[0];
  }

  const api = { ordered, next, firstAfter };
  globalScope.CadsceneSuggestionSequence = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
