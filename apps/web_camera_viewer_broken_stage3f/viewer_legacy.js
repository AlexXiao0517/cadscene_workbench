(function () {
  "use strict";

  const paths = window.CADSCENE_VIEWER_PATHS || {};
  const fallback = window.CadsceneFallback;
  const state = {
    track: null,
    qualityTimeline: [],
    suggestions: [],
    sfmScene: null,
    diagnosticsScene: null,
    manualFrames: [],
    currentFrame: 0,
    three: {},
  };

  function isManualKeyframe(item) {
    const source = String(item && item.source ? item.source : "");
    return source === "manual_keyframe" || source === "confirmed_keyframe" || source === "manual" || source === "confirmed";
  }

  function isAlgorithmPrediction(item) {
    return String(item && item.source ? item.source : "") === "algorithm_prediction";
  }

  function getFrame(item) {
    return Number(item.frame ?? item.frame_index ?? 0);
  }

  function normalizeSuggestions(data) {
    if (!data) return [];
    return Array.isArray(data) ? data : data.suggestions || [];
  }

  async function loadTrack() {
    const track = (await fallback.fetchJson(paths.qualityTrack, true)) || (await fallback.fetchJson(paths.track, true)) || { keyframes: [] };
    state.track = track;
    state.manualFrames = (track.keyframes || []).filter((item) => !isAlgorithmPrediction(item) && isManualKeyframe(item)).map(getFrame).sort((a, b) => a - b);
    return track;
  }

  async function loadQualityTimeline() {
    const csv = await fallback.fetchText(paths.qualityTimeline, true);
    state.qualityTimeline = fallback.parseCsv(csv).map((row) => ({
      frame_index: Number(row.frame_index || 0),
      risk_score: Number(row.risk_score || 0),
      risk_level: row.risk_level || "low",
      reason_codes: row.reason_codes || "",
    }));
  }

  async function loadSuggestions() {
    state.suggestions = normalizeSuggestions(await fallback.fetchJson(paths.suggestions, true));
  }

  async function loadSfmScene() {
    state.sfmScene = await fallback.fetchJson(paths.sfmScene, true);
  }

  async function loadDiagnosticsScene() {
    state.diagnosticsScene = await fallback.fetchJson(paths.diagnosticsScene, true);
  }

  function cameraFromTrackKeyframe(frame) {
    const keyframes = (state.track && state.track.keyframes) || [];
    const found = keyframes.find((item) => getFrame(item) === frame && item.camera);
    return found ? found.camera : null;
  }

  function anchoredTrackForDisplay() {
    const trackKeyframes = ((state.track && state.track.keyframes) || []).filter((item) => item.camera);
    if (trackKeyframes.length) {
      return trackKeyframes.map((item) => ({ frame_index: getFrame(item), camera: item.camera, source: item.source || "track" }));
    }
    return (((state.sfmScene || {}).tracks || {}).anchored_camera_path || []);
  }

  function setStatus() {
    const lines = [
      `dataset: ${paths.dataset || ""}`,
      `runId: ${paths.runId || ""}`,
      `track keyframes: ${((state.track || {}).keyframes || []).length}`,
      `manual/confirmed: ${state.manualFrames.length}`,
      `qualityTimeline: ${state.qualityTimeline.length}`,
      `suggestions: ${state.suggestions.length}`,
      `sfmScene points: ${(((state.sfmScene || {}).points || {}).data || []).length}`,
      `diagnostics warnings: ${((state.diagnosticsScene || {}).warnings || []).length}`,
      "suggestions 只提示和跳转，不自动添加关键帧或修改相机。",
      "SfM 点云和 diagnostics scene 均为只读展示。",
      "RGB 按 0..255 整数解释。",
    ];
    document.getElementById("status").textContent = lines.join("\n");
    document.getElementById("datasetLabel").textContent = [paths.dataset, paths.runId].filter(Boolean).join(" / ");
  }

  function riskColor(level) {
    if (level === "high") return "#e55a5a";
    if (level === "medium") return "#d7a941";
    return "#4fa36a";
  }

  function drawTimeline() {
    const canvas = document.getElementById("timeline");
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#171b22";
    ctx.fillRect(0, 0, w, h);
    const frames = [
      ...state.qualityTimeline.map((row) => row.frame_index),
      ...state.suggestions.map((row) => Number(row.frame_index || 0)),
      ...state.manualFrames,
    ];
    const maxFrame = Math.max(1, ...frames);
    state.qualityTimeline.forEach((row) => {
      const x = Math.round((row.frame_index / maxFrame) * w);
      ctx.fillStyle = riskColor(row.risk_level);
      ctx.globalAlpha = 0.45;
      ctx.fillRect(x, 10, Math.max(2, w / maxFrame), 30);
    });
    ctx.globalAlpha = 1;
    state.suggestions.forEach((row) => {
      const x = Math.round((Number(row.frame_index || 0) / maxFrame) * w);
      ctx.fillStyle = "#ffffff";
      ctx.beginPath();
      ctx.moveTo(x, 48);
      ctx.lineTo(x - 5, 70);
      ctx.lineTo(x + 5, 70);
      ctx.closePath();
      ctx.fill();
    });
    state.manualFrames.forEach((frame) => {
      const x = Math.round((frame / maxFrame) * w);
      ctx.fillStyle = "#5aa7ff";
      ctx.fillRect(x - 2, 4, 4, h - 8);
    });
    ctx.fillStyle = "#f5f7fb";
    const playhead = Math.round((state.currentFrame / maxFrame) * w);
    ctx.fillRect(playhead, 0, 2, h);
  }

  function initScene() {
    const host = document.getElementById("scene3d");
    if (!window.THREE || !host) return;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(55, host.clientWidth / Math.max(host.clientHeight, 1), 0.1, 100000);
    camera.position.set(0, -120, 80);
    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(host.clientWidth, host.clientHeight);
    host.innerHTML = "";
    host.appendChild(renderer.domElement);
    const controls = window.THREE.OrbitControls ? new THREE.OrbitControls(camera, renderer.domElement) : null;
    state.three = { scene, camera, renderer, controls, points: null, diagnostics: null };
    scene.add(new THREE.AxesHelper(20));
    animateScene();
  }

  function addPointCloud() {
    if (!window.THREE || !state.sfmScene || !state.three.scene) return;
    const rows = (((state.sfmScene || {}).points || {}).data || []);
    if (!rows.length) return;
    const geometry = new THREE.BufferGeometry();
    const positions = [];
    const colors = [];
    rows.forEach((row) => {
      positions.push(Number(row[0] || 0), Number(row[1] || 0), Number(row[2] || 0));
      colors.push(Number(row[3] || 255) / 255, Number(row[4] || 255) / 255, Number(row[5] || 255) / 255);
    });
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    const material = new THREE.PointsMaterial({ size: Number(document.getElementById("pointSize").value || 2), vertexColors: true });
    state.three.points = new THREE.Points(geometry, material);
    state.three.scene.add(state.three.points);
  }

  function addDiagnosticsScene() {
    if (!window.THREE || !state.diagnosticsScene || !state.three.scene) return;
    const points = state.diagnosticsScene.road_points || [];
    if (!points.length) return;
    const geometry = new THREE.BufferGeometry();
    const positions = [];
    points.forEach((row) => positions.push(Number(row[0] || row.x || 0), Number(row[1] || row.y || 0), Number(row[2] || row.z || 0)));
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({ size: 3, color: 0xffaa55 });
    state.three.diagnostics = new THREE.Points(geometry, material);
    state.three.scene.add(state.three.diagnostics);
  }

  function animateScene() {
    const t = state.three;
    if (!t.renderer) return;
    requestAnimationFrame(animateScene);
    if (t.controls) t.controls.update();
    t.renderer.render(t.scene, t.camera);
  }

  function bindControls() {
    const video = document.getElementById("video");
    if (paths.video) video.src = paths.video;
    document.getElementById("prevKeyframe").addEventListener("click", () => {
      const prev = [...state.manualFrames].reverse().find((frame) => frame < state.currentFrame);
      if (prev !== undefined) state.currentFrame = prev;
      drawTimeline();
    });
    document.getElementById("nextKeyframe").addEventListener("click", () => {
      const next = state.manualFrames.find((frame) => frame > state.currentFrame);
      if (next !== undefined) state.currentFrame = next;
      drawTimeline();
    });
    document.getElementById("timeline").addEventListener("click", (event) => {
      const rect = event.currentTarget.getBoundingClientRect();
      const frames = [...state.manualFrames, ...state.suggestions.map((row) => Number(row.frame_index || 0))];
      const maxFrame = Math.max(1, ...frames);
      state.currentFrame = Math.round(((event.clientX - rect.left) / rect.width) * maxFrame);
      drawTimeline();
    });
    document.getElementById("togglePoints").addEventListener("change", (event) => {
      if (state.three.points) state.three.points.visible = event.target.checked;
    });
    document.getElementById("toggleDiagnostics").addEventListener("change", (event) => {
      if (state.three.diagnostics) state.three.diagnostics.visible = event.target.checked;
    });
    document.getElementById("pointSize").addEventListener("input", (event) => {
      if (state.three.points) state.three.points.material.size = Number(event.target.value);
    });
  }

  async function boot() {
    bindControls();
    await Promise.all([loadTrack(), loadQualityTimeline(), loadSuggestions(), loadSfmScene(), loadDiagnosticsScene()]);
    initScene();
    addPointCloud();
    addDiagnosticsScene();
    anchoredTrackForDisplay();
    cameraFromTrackKeyframe(state.currentFrame);
    drawTimeline();
    setStatus();
  }

  window.CadsceneViewerLegacy = {
    isManualKeyframe,
    isAlgorithmPrediction,
    loadSuggestions,
    loadQualityTimeline,
    loadSfmScene,
    loadDiagnosticsScene,
    anchoredTrackForDisplay,
  };

  document.addEventListener("DOMContentLoaded", () => {
    boot().catch((err) => {
      document.getElementById("status").textContent = String(err && err.stack ? err.stack : err);
    });
  });
})();
